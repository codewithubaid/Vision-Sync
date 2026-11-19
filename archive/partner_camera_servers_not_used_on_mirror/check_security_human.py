"""Local camera-only Security Mode diagnostic; never sends notifications or calls.

Usage: python check_security_human.py --camera 1
       python check_security_human.py --camera 0

This test uses OpenCV's front-face cascade, which is NOT reliable full-body
human detection. A turned head or back-facing person can be missed.
"""
import argparse
import time

import cv2


def main():
    parser = argparse.ArgumentParser(description="Safe local human-presence diagnostic")
    parser.add_argument("--camera", type=int, default=1, help="USB webcam camera index")
    args = parser.parse_args()

    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    if cascade.empty():
        raise RuntimeError("OpenCV frontal-face model is unavailable")

    cap = cv2.VideoCapture(args.camera, cv2.CAP_DSHOW if hasattr(cv2, "CAP_DSHOW") else cv2.CAP_ANY)
    if not cap.isOpened():
        raise RuntimeError("Cannot access webcam. Close the stream server, then try --camera 0 or 1")
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    prev = None
    last_seen = None
    last_check = 0.0
    faces = ()
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                print("Camera stopped delivering frames")
                break
            now = time.monotonic()
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            smooth = cv2.GaussianBlur(gray, (21, 21), 0)
            motion = False
            if prev is not None:
                delta = cv2.absdiff(prev, smooth)
                _, mask = cv2.threshold(delta, 25, 255, cv2.THRESH_BINARY)
                mask = cv2.dilate(mask, None, iterations=2)
                contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                motion = any(cv2.contourArea(c) >= 1000 for c in contours)
            prev = smooth

            if now - last_check >= 0.5:
                last_check = now
                faces = cascade.detectMultiScale(gray, 1.1, 5, minSize=(35, 35))
                if len(faces):
                    last_seen = now
            for x, y, w, h in faces:
                cv2.rectangle(frame, (x, y), (x+w, y+h), (0, 255, 0), 2)

            human_recent = last_seen is not None and (now - last_seen <= 2.5)
            if motion and human_recent:
                status, color = "HUMAN + MOTION (face confirmed)", (0, 255, 0)
            elif motion:
                status, color = "MOTION ONLY (human not confirmed)", (0, 165, 255)
            elif human_recent:
                status, color = "FACE VISIBLE / NO MOTION", (0, 255, 255)
            else:
                status, color = "NO HUMAN CONFIRMED", (200, 200, 200)
            cv2.putText(frame, status, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, .55, color, 2)
            cv2.putText(frame, "Diagnostic only: Q to quit / NO ALERTS", (10, 55),
                        cv2.FONT_HERSHEY_SIMPLEX, .46, (255, 255, 255), 1)
            cv2.imshow("VisionSync Security Human Diagnostic", frame)
            if cv2.waitKey(1) & 0xFF in (ord('q'), 27):
                break
    finally:
        cap.release()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
