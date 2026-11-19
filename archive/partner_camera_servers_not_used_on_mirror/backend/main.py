import asyncio
from contextlib import asynccontextmanager
import logging
import time

from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field, StrictBool

from .auth import Authorizer
from .camera import Camera
from .config import Settings
from .database import Database
from .notifications import NotificationWorker

log = logging.getLogger(__name__)


class CameraSettings(BaseModel):
    camera_enabled: StrictBool | None = None
    security_enabled: StrictBool | None = None


class Device(BaseModel):
    installation_id: str = Field(min_length=20, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    enabled: StrictBool = True


def create_app(config=None, start_camera=True):
    config = config or Settings.from_env()
    database = Database(config.database_path)
    camera = Camera(config, database)
    notifier = NotificationWorker(config, database)
    authorizer = Authorizer(config)
    test_times = {}

    @asynccontextmanager
    async def lifespan(app):
        if start_camera and config.auth_configured:
            camera.start()
            notifier.start()
        elif not config.auth_configured:
            log.warning("Set backend/.env with Supabase settings and allowed user IDs before connecting")
        try:
            yield
        finally:
            camera.stop()
            notifier.stop()
            await authorizer.close()
            # Avoid closing a connection under a driver/SDK call still winding down.
            if not (camera.thread and camera.thread.is_alive()) and not (notifier.thread and notifier.thread.is_alive()):
                database.close()

    app = FastAPI(title="Glass Connect Camera", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.database, app.state.camera = database, camera
    app.state.notifier, app.state.authorizer = notifier, authorizer

    async def require_user(authorization: str | None = Header(default=None)):
        return await authorizer.user(authorization)

    app.state.require_user = require_user

    def camera_exists(camera_id):
        if camera_id != config.camera_id:
            raise HTTPException(404, "Camera not found")

    def status(user_id):
        history = database.list_incidents(user_id, limit=1)
        return {
            **camera.status(), "incident_count": history["total"],
            "latest_incident": history["items"][0] if history["items"] else None,
            "push_ready": notifier.ready, "push_error": notifier.error,
        }

    @app.get("/health")
    def health():
        return {"configured": config.auth_configured}

    @app.get("/cameras")
    async def cameras(user_id=Depends(require_user)):
        return {"items": [status(user_id)]}

    @app.get("/cameras/{camera_id}/status")
    async def camera_status(camera_id: str, user_id=Depends(require_user)):
        camera_exists(camera_id)
        return status(user_id)

    @app.patch("/cameras/{camera_id}/settings")
    async def update_settings(camera_id: str, settings: CameraSettings, user_id=Depends(require_user)):
        camera_exists(camera_id)
        camera.update(settings.camera_enabled, settings.security_enabled)
        return status(user_id)

    @app.get("/cameras/{camera_id}/stream")
    async def stream(camera_id: str, authorization: str = Header(), user_id=Depends(require_user)):
        camera_exists(camera_id)
        if not camera.status()["camera_online"]:
            raise HTTPException(503, "Camera is offline or starting")

        async def frames():
            sequence = -1
            verified_at = time.monotonic()
            while not camera.stop_event.is_set():
                if time.monotonic() - verified_at > 30:
                    try:
                        await authorizer.user(authorization)
                    except HTTPException:
                        return
                    verified_at = time.monotonic()
                current_sequence, jpeg = camera.frame()
                if jpeg is None:
                    return
                if current_sequence != sequence:
                    sequence = current_sequence
                    yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"
                await asyncio.sleep(0.03)

        return StreamingResponse(frames(), media_type="multipart/x-mixed-replace; boundary=frame",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.get("/incidents")
    async def incidents(limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0),
                        user_id=Depends(require_user)):
        return database.list_incidents(user_id, limit, offset)

    @app.get("/incidents/{incident_id}")
    async def incident(incident_id: str, user_id=Depends(require_user)):
        result = database.incident(incident_id, user_id)
        if result is None:
            raise HTTPException(404, "Incident not found")
        return result

    @app.post("/incidents/{incident_id}/read")
    async def read_incident(incident_id: str, user_id=Depends(require_user)):
        if database.incident(incident_id, user_id) is None:
            raise HTTPException(404, "Incident not found")
        database.mark_read(incident_id, user_id)
        return {"read": True}

    @app.get("/incidents/{incident_id}/snapshot")
    async def snapshot(incident_id: str, user_id=Depends(require_user)):
        jpeg = database.snapshot(incident_id, user_id)
        if jpeg is None:
            raise HTTPException(404, "Snapshot not available")
        return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.post("/devices")
    async def register_device(device: Device, user_id=Depends(require_user)):
        database.register_device(user_id, device.installation_id, device.enabled)
        return {"registered": True, "enabled": device.enabled}

    @app.delete("/devices")
    async def remove_device(device: Device, user_id=Depends(require_user)):
        database.remove_device(user_id, device.installation_id)
        return {"removed": True}

    @app.post("/notifications/test")
    async def test_notification(user_id=Depends(require_user)):
        if not notifier.ready:
            raise HTTPException(503, notifier.error or "Firebase has not been configured on the server")
        if not database.device_count(user_id):
            raise HTTPException(409, "Enable notifications on this phone first")
        if time.monotonic() - test_times.get(user_id, -100) < 30:
            raise HTTPException(429, "Wait 30 seconds before another test")
        _, jpeg = camera.frame()
        event_id = database.create_incident(config, jpeg, "test_notification", target_user=user_id)
        test_times[user_id] = time.monotonic()
        return {"incident_id": event_id, "delivery": "queued"}

    @app.websocket("/updates")
    async def updates(socket: WebSocket):
        authorization = socket.headers.get("authorization")
        try:
            user_id = await authorizer.user(authorization)
        except HTTPException:
            await socket.close(code=1008)
            return
        await socket.accept()
        verified_at = time.monotonic()
        try:
            while True:
                if time.monotonic() - verified_at > 30:
                    user_id = await authorizer.user(authorization)
                    verified_at = time.monotonic()
                await socket.send_json({"type": "state", "camera": status(user_id)})
                await asyncio.sleep(2)
        except WebSocketDisconnect:
            pass
        except HTTPException:
            await socket.close(code=1008)
        except RuntimeError:
            pass

    return app
