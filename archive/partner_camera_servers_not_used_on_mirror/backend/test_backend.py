"""Offline regressions for authentication, durable delivery, and capture lifecycle."""

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import Mock, PropertyMock, patch

import httpx
import numpy as np
from fastapi import HTTPException
from fastapi.testclient import TestClient
from firebase_admin import messaging

from .auth import Authorizer
from .camera import Camera
from .config import Settings
from .database import Database
from .main import create_app
from .notifications import NotificationWorker


ALICE = "alice"
BOB = "bob"
FID = "c123456789012345678901"


def configuration(path):
    return Settings(
        database_path=Path(path), supabase_url="https://example.supabase.co",
        supabase_key="test-publishable-key", allowed_users=frozenset({ALICE, BOB}),
        fcm_enabled=True,
    )


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.config = configuration(Path(self.folder.name) / "camera.sqlite3")
        self.database = Database(self.config.database_path)

    def tearDown(self):
        self.database.close()
        self.folder.cleanup()

    def test_incident_snapshot_read_state_and_settings_survive_restart(self):
        incident_id = self.database.create_incident(self.config, b"jpeg")
        self.database.mark_read(incident_id, ALICE)
        self.database.save_settings({"camera_enabled": False})
        self.database.close()
        self.database = Database(self.config.database_path)
        incident = self.database.incident(incident_id, ALICE)
        self.assertIsNone(incident["confidence"])
        self.assertFalse(incident["human_confirmed"])
        self.assertTrue(incident["read"])
        self.assertFalse(self.database.incident(incident_id, BOB)["read"])
        self.assertEqual(self.database.snapshot(incident_id, ALICE), b"jpeg")
        self.assertFalse(self.database.setting("camera_enabled", True))

    def test_private_test_has_private_history_snapshot_and_read_state(self):
        incident_id = self.database.create_incident(
            self.config, b"private", "test_notification", target_user=ALICE,
        )
        self.assertEqual(self.database.list_incidents(ALICE)["total"], 1)
        self.assertEqual(self.database.list_incidents(BOB)["total"], 0)
        self.assertIsNone(self.database.incident(incident_id, BOB))
        self.assertIsNone(self.database.snapshot(incident_id, BOB))
        self.database.mark_read(incident_id, BOB)
        self.assertEqual(self.database.connection.execute("SELECT count(*) FROM incident_reads").fetchone()[0], 0)

    def test_registration_is_idempotent_and_owner_change_cancels_jobs(self):
        self.database.register_device(ALICE, FID, True)
        self.database.register_device(ALICE, FID, True)
        incident_id = self.database.create_incident(self.config, None)
        self.assertEqual(self.database.device_count(ALICE), 1)
        self.database.register_device(BOB, FID, True)
        self.assertIsNone(self.database.next_delivery())
        self.assertEqual(self.database.incident(incident_id, ALICE)["delivery"], {"cancelled": 1})
        self.database.remove_device(ALICE, FID)
        self.assertEqual(self.database.device_count(BOB), 1)

    def test_disable_cancels_a_claimed_job_and_late_result_cannot_overwrite(self):
        self.database.register_device(ALICE, FID, True)
        incident_id = self.database.create_incident(self.config, None)
        job = self.database.next_delivery()
        self.database.register_device(ALICE, FID, False)
        self.database.delivery_result(job)
        self.assertEqual(self.database.incident(incident_id, ALICE)["delivery"], {"cancelled": 1})
        self.assertIsNone(self.database.next_delivery())

    def test_removal_cancels_pending_jobs(self):
        self.database.register_device(ALICE, FID, True)
        incident_id = self.database.create_incident(self.config, None)
        self.database.remove_device(ALICE, FID)
        self.assertEqual(self.database.device_count(ALICE), 0)
        self.assertEqual(self.database.incident(incident_id, ALICE)["delivery"], {"cancelled": 1})

    def test_jobs_are_only_created_for_enabled_allowed_target_user(self):
        self.database.register_device(ALICE, FID, True)
        self.database.register_device(BOB, "b" * 22, True)
        self.database.register_device("intruder", "i" * 22, True)
        self.database.register_device(ALICE, "d" * 22, False)
        self.database.create_incident(self.config, None, "test_notification", target_user=ALICE)
        self.assertEqual(self.database.next_delivery()["user_id"], ALICE)
        self.assertIsNone(self.database.next_delivery())

    def test_lease_prevents_duplicate_claim_and_recovers_after_restart(self):
        self.database.register_device(ALICE, FID, True)
        self.database.create_incident(self.config, None)
        first = self.database.next_delivery()
        other = Database(self.config.database_path)
        try:
            self.assertIsNone(other.next_delivery())
            with self.database.connection:
                self.database.connection.execute("UPDATE deliveries SET next_attempt=0")
            second = other.next_delivery()
            self.assertEqual(second["attempts"], 2)
            self.database.delivery_result(first)
            self.assertEqual(self.database.incident(first["incident_id"], ALICE)["delivery"], {"sending": 1})
            other.delivery_result(second)
            self.assertEqual(self.database.incident(first["incident_id"], ALICE)["delivery"], {"sent": 1})
        finally:
            other.close()

    def test_transient_failures_back_off_and_stop_after_five_attempts(self):
        self.database.register_device(ALICE, FID, True)
        incident_id = self.database.create_incident(self.config, None)
        for attempt in range(1, 6):
            job = self.database.next_delivery()
            self.assertEqual(job["attempts"], attempt)
            self.database.delivery_result(job, "UnavailableError")
            self.assertIsNone(self.database.next_delivery())
            with self.database.connection:
                self.database.connection.execute("UPDATE deliveries SET next_attempt=0")
        self.assertEqual(self.database.incident(incident_id, ALICE)["delivery"], {"failed": 1})

    def test_legacy_database_migrates_private_test_visibility(self):
        self.database.close()
        with sqlite3.connect(self.config.database_path) as connection:
            connection.execute("ALTER TABLE incidents DROP COLUMN target_user")
            connection.execute(
                "INSERT INTO incidents(id,camera_id,camera_name,type,timestamp) VALUES('old','1','Room','test_notification','now')"
            )
        self.database = Database(self.config.database_path)
        self.assertEqual(self.database.list_incidents(ALICE)["total"], 0)


class AuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.authorizer = Authorizer(configuration(":memory:"))
        await self.authorizer.client.aclose()

    async def asyncTearDown(self):
        await self.authorizer.close()

    def response(self, status, payload):
        self.authorizer.client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(status, json=payload),
        ))

    async def assert_status(self, authorization, status):
        with self.assertRaises(HTTPException) as raised:
            await self.authorizer.user(authorization)
        self.assertEqual(raised.exception.status_code, status)

    async def test_missing_or_malformed_bearer_is_rejected(self):
        for header in (None, "Basic abc", "Bearer ", "Bearer two words"):
            await self.assert_status(header, 401)

    async def test_verified_owner_is_cached(self):
        self.response(200, {"id": ALICE})
        self.assertEqual(await self.authorizer.user("bearer valid"), ALICE)
        await self.authorizer.client.aclose()
        self.assertEqual(await self.authorizer.user("Bearer valid"), ALICE)

    async def test_expired_and_unlisted_users_are_rejected(self):
        self.response(401, {})
        await self.assert_status("Bearer expired", 401)
        await self.authorizer.client.aclose()
        self.response(200, {"id": "intruder"})
        await self.assert_status("Bearer valid", 403)

    async def test_malformed_verification_response_is_service_error(self):
        self.response(200, ["invalid"])
        await self.assert_status("Bearer valid", 503)

    async def test_unconfigured_server_fails_closed(self):
        self.authorizer.config = Settings()
        await self.assert_status("Bearer valid", 503)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.config = configuration(Path(self.folder.name) / "camera.sqlite3")
        self.app = create_app(self.config, start_camera=False)
        asyncio.run(self.app.state.authorizer.client.aclose())
        self.app.state.authorizer.client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"id": request.headers["authorization"].split()[1]}),
        ))
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.headers = {"Authorization": "Bearer alice"}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.folder.cleanup()

    def test_camera_stream_incidents_and_settings_require_an_owner(self):
        for route in ("/cameras", "/cameras/1/status", "/cameras/1/stream", "/incidents"):
            self.assertIn(self.client.get(route).status_code, (401, 422))
            self.assertEqual(self.client.get(route, headers={"Authorization": "Bearer intruder"}).status_code, 403)
        self.assertEqual(self.client.patch("/cameras/1/settings", json={"camera_enabled": False}).status_code, 401)

    def test_device_contract_and_disable_persist(self):
        payload = {"installation_id": FID, "enabled": True}
        self.assertEqual(self.client.post("/devices", headers=self.headers, json=payload).status_code, 200)
        self.assertEqual(self.app.state.database.device_count(ALICE), 1)
        payload["enabled"] = False
        self.assertEqual(self.client.post("/devices", headers=self.headers, json=payload).status_code, 200)
        self.assertEqual(self.app.state.database.device_count(ALICE), 0)
        self.assertEqual(self.client.request("DELETE", "/devices", headers=self.headers, json=payload).status_code, 200)
        self.assertEqual(self.client.post("/devices", headers=self.headers, json={"token": FID}).status_code, 422)

    def test_camera_off_disarms_and_clears_frame(self):
        camera = self.app.state.camera
        camera.jpeg, camera.last_frame, camera.motion = b"jpeg", time.monotonic(), True
        response = self.client.patch("/cameras/1/settings", headers=self.headers,
                                     json={"camera_enabled": False, "security_enabled": True})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["camera_enabled"])
        self.assertFalse(response.json()["security_enabled"])
        self.assertFalse(response.json()["camera_online"])
        self.assertIsNone(camera.frame()[1])
        self.assertEqual(self.client.get("/cameras/1/stream", headers=self.headers).status_code, 503)
        self.assertEqual(self.client.patch("/cameras/1/settings", headers=self.headers,
                                          json={"security_enabled": "true"}).status_code, 422)

    def test_incident_read_and_snapshot(self):
        incident_id = self.app.state.database.create_incident(self.config, b"jpeg")
        response = self.client.get(f"/incidents/{incident_id}/snapshot", headers=self.headers)
        self.assertEqual(response.content, b"jpeg")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.client.post(f"/incidents/{incident_id}/read", headers=self.headers).status_code, 200)
        self.assertTrue(self.client.get(f"/incidents/{incident_id}", headers=self.headers).json()["read"])

    def test_notification_test_queues_only_requester_and_rate_limits(self):
        self.app.state.database.register_device(ALICE, FID, True)
        self.app.state.database.register_device(BOB, "b" * 22, True)
        with patch.object(NotificationWorker, "ready", new_callable=PropertyMock, return_value=True):
            response = self.client.post("/notifications/test", headers=self.headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["delivery"], "queued")
            incident_id = response.json()["incident_id"]
            self.assertEqual(self.client.post("/notifications/test", headers=self.headers).status_code, 429)
        self.assertEqual(self.app.state.database.next_delivery()["user_id"], ALICE)
        self.assertIsNone(self.app.state.database.next_delivery())
        bob_headers = {"Authorization": "Bearer bob"}
        self.assertEqual(self.client.get("/incidents", headers=bob_headers).json()["total"], 0)
        self.assertEqual(self.client.get(f"/incidents/{incident_id}", headers=bob_headers).status_code, 404)
        self.assertEqual(self.client.get(f"/incidents/{incident_id}/snapshot", headers=bob_headers).status_code, 404)

    def test_websocket_requires_owner_and_emits_camera_state(self):
        from starlette.websockets import WebSocketDisconnect
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect("/updates"):
                pass
        with self.client.websocket_connect("/updates", headers=self.headers) as socket:
            message = socket.receive_json()
            self.assertEqual(message["type"], "state")
            self.assertEqual(message["camera"]["camera_id"], "1")


class CameraTests(DatabaseTests):
    def test_capture_publishes_incident_and_releases_mock_camera(self):
        camera = Camera(self.config, self.database)
        camera.update(security_enabled=True)
        capture = Mock()
        capture.read.return_value = (True, np.zeros((480, 640, 3), dtype=np.uint8))
        detector = Mock()
        detector.process.return_value = (True, True)
        with patch.object(camera, "open_camera", return_value=capture), \
                patch("backend.camera.MotionDetector", return_value=detector), \
                patch.object(camera.stop_event, "wait", side_effect=lambda seconds: camera.stop_event.set()):
            camera.run()
        self.assertEqual(self.database.list_incidents(ALICE)["total"], 1)
        capture.release.assert_called_once()
        self.assertIsNone(camera.frame()[1])

    def test_disable_during_capture_prevents_stale_publication(self):
        camera = Camera(self.config, self.database)
        camera.update(security_enabled=True)
        capture = Mock()

        def read():
            camera.update(camera_enabled=False)
            camera.stop_event.set()
            return True, np.zeros((480, 640, 3), dtype=np.uint8)

        capture.read.side_effect = read
        detector = Mock()
        detector.process.return_value = (True, True)
        with patch.object(camera, "open_camera", return_value=capture), \
                patch("backend.camera.MotionDetector", return_value=detector):
            camera.run()
        self.assertEqual(self.database.list_incidents(ALICE)["total"], 0)
        self.assertIsNone(camera.frame()[1])
        capture.release.assert_called_once()

    def test_off_camera_never_opens_hardware(self):
        camera = Camera(self.config, self.database)
        camera.update(camera_enabled=False)
        with patch.object(camera, "open_camera") as open_camera, \
                patch.object(camera.stop_event, "wait", side_effect=lambda seconds: camera.stop_event.set()):
            camera.run()
        open_camera.assert_not_called()


class NotificationTests(DatabaseTests):
    def test_fid_notification_payload_and_success_are_persisted(self):
        self.database.register_device(ALICE, FID, True)
        incident_id = self.database.create_incident(self.config, b"jpeg")
        worker = NotificationWorker(self.config, self.database)
        encoded = []

        def send(message, app):
            encoded.append(json.loads(str(message)))
            worker.stop_event.set()
            return "message-id"

        with patch.object(messaging, "send", side_effect=send):
            worker.run()
        self.assertEqual(encoded[0]["fid"], FID)
        self.assertNotIn("token", encoded[0])
        self.assertEqual(encoded[0]["data"]["incident_id"], incident_id)
        self.assertEqual(encoded[0]["notification"]["title"], "Movement detected")
        self.assertEqual(encoded[0]["android"]["notification"]["channel_id"], "camera_alerts")
        self.assertEqual(self.database.incident(incident_id, ALICE)["delivery"], {"sent": 1})

    def test_unregistered_installation_is_removed(self):
        self.database.register_device(ALICE, FID, True)
        incident_id = self.database.create_incident(self.config, None)
        worker = NotificationWorker(self.config, self.database)

        def send(message, app):
            worker.stop_event.set()
            raise messaging.UnregisteredError("expired")

        with patch.object(messaging, "send", side_effect=send):
            worker.run()
        self.assertEqual(self.database.device_count(ALICE), 0)
        self.assertEqual(self.database.incident(incident_id, ALICE)["delivery"], {"failed": 1})


if __name__ == "__main__":
    unittest.main()
