"""Step 1: share one camera between live streaming and basic motion detection."""

import argparse
import os
import threading
import time

import cv2
from flask import Flask, Response, jsonify, request

from security_events import SecurityEventTracker


app = Flask(__name__)
condition = threading.Condition()
stopping = threading.Event()
state = {"jpeg": None, "sequence": 0, "online": False, "motion": False}
# Prototype tuning, not SRS-mandated times: 2s movement, short gaps allowed,
# at most one event per episode and at least 10s between events.
security = SecurityEventTracker()


def find_camera(index, backend):
    # Select one camera explicitly; never silently switch to another room/device.
    camera = cv2.VideoCapture(index, backend)

    if camera.isOpened():
        print(f"Camera opened at index {index} using {camera.getBackendName()}")
        return camera

    camera.release()
    return None


def capture_frames(camera):
    """Only this worker reads the camera, even when several viewers connect."""
    previous_gray = None
    try:
        while not stopping.is_set():
            success, frame = camera.read()
            if not success or frame is None:
                raise RuntimeError("Camera stopped sending images.")

            frame = cv2.resize(frame, (640, 480))
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            gray = cv2.GaussianBlur(gray, (21, 21), 0)
            motion = False
            label = "Starting..."

            if previous_gray is not None:
                difference = cv2.absdiff(previous_gray, gray)
                _, mask = cv2.threshold(difference, 25, 255, cv2.THRESH_BINARY)
                mask = cv2.dilate(mask, None, iterations=2)
                contours, _ = cv2.findContours(
                    mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
                )
                for contour in contours:
                    if cv2.contourArea(contour) < 1000:
                        continue
                    motion = True
                    x, y, width, height = cv2.boundingRect(contour)
                    cv2.rectangle(
                        frame, (x, y), (x + width, y + height), (0, 0, 255), 2
                    )
                label = "Motion detected" if motion else "No motion"

            # Keep the unannotated image for the next comparison.
            previous_gray = gray
            color = (0, 0, 255) if motion else (0, 255, 0)
            cv2.putText(
                frame, label, (15, 35), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2
            )
            encoded, buffer = cv2.imencode(
                ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 70]
            )
            if not encoded:
                raise RuntimeError("Could not encode the camera image.")

            # Publish one finished frame; viewers never call camera.read().
            with condition:
                state.update(jpeg=buffer.tobytes(), online=True, motion=motion)
                state["sequence"] += 1
                event = security.observe(motion, time.monotonic())
                condition.notify_all()
            if event is not None:
                print(
                    f"Motion candidate #{event['event_id']} at {event['timestamp']} "
                    "(human verification pending; phone notification not sent).",
                    flush=True,
                )
            stopping.wait(1 / 15)
    except Exception as error:
        print(f"Camera error: {error}")
    finally:
        camera.release()
        stopping.set()
        with condition:
            state.update(jpeg=None, online=False, motion=False)
            security.reset_pending()
            condition.notify_all()


def generate_frames():
    last_sequence = -1
    while not stopping.is_set():
        with condition:
            condition.wait_for(
                lambda: stopping.is_set() or state["sequence"] != last_sequence
            )
            if stopping.is_set() or not state["online"]:
                return
            jpeg = state["jpeg"]
            last_sequence = state["sequence"]
        yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"


@app.route("/video_feed")
def video_feed():
    with condition:
        if not state["online"]:
            return "Camera is starting or offline. Try again shortly.", 503
    return Response(
        generate_frames(), mimetype="multipart/x-mixed-replace; boundary=frame"
    )


@app.route("/status")
def status():
    with condition:
        return jsonify(
            camera_online=state["online"], motion_detected=state["motion"],
            security_enabled=security.enabled, session_id=security.session_id,
            latest_event_id=security.latest_event_id,
        )


@app.route("/security-mode", methods=["GET", "POST"])
def security_mode():
    payload = request.get_json(silent=True) if request.method == "POST" else None
    if request.method == "POST":
        if not isinstance(payload, dict) or type(payload.get("enabled")) is not bool:
            return jsonify(error='Send JSON with "enabled": true or false.'), 400
    with condition:
        if payload is not None:
            security.set_enabled(payload["enabled"])
        return jsonify(enabled=security.enabled, camera_online=state["online"])


@app.route("/events")
def events():
    try:
        after_id = int(request.args.get("after_id", "0"))
        if after_id < 0:
            raise ValueError
    except ValueError:
        return jsonify(error="after_id must be a non-negative integer."), 400
    with condition:
        retained = security.events_after()
        return jsonify(
            session_id=security.session_id,
            latest_event_id=security.latest_event_id,
            oldest_event_id=retained[0]["event_id"] if retained else None,
            events=security.events_after(after_id),
        )


@app.route("/")
def index():
    return '<h1>Camera + Motion Test</h1><img src="/video_feed" width="640">'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--camera", type=int, default=1,
        help="Camera index (default: 1). Try 0 for the laptop camera; verify the image.",
    )
    parser.add_argument(
        "--backend", choices=("auto", "dshow", "msmf"),
        default="dshow" if os.name == "nt" else "auto",
        help="Capture method (default: dshow on Windows, auto elsewhere).",
    )
    args = parser.parse_args()
    if args.camera < 0:
        parser.error("--camera must be zero or greater")
    if os.name != "nt" and args.backend != "auto":
        parser.error("dshow and msmf are Windows-only; use --backend auto")
    backends = {"auto": cv2.CAP_ANY, "dshow": cv2.CAP_DSHOW, "msmf": cv2.CAP_MSMF}
    camera = find_camera(args.camera, backends[args.backend])
    if camera is None:
        print(
            f"Could not open camera index {args.camera} using {args.backend}. "
            "Close other camera programs and check the selected camera index and USB connection."
        )
        return
    camera.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    worker = threading.Thread(target=capture_frames, args=(camera,), daemon=True)
    worker.start()
    try:
        app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
    finally:
        stopping.set()
        with condition:
            condition.notify_all()
        worker.join(timeout=2)


if __name__ == "__main__":
    main()
