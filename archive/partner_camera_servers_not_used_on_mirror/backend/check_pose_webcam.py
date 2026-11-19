"""Local MediaPipe webcam preview. No server, alerts, phone calls or data storage.

Run from camera_app directory:
  python -m backend.check_pose_webcam --camera 1 --model backend/models/pose_landmarker_lite.task
Press q to quit. Stop the camera server first to avoid device contention.
"""
import argparse
import time


def main():
    parser = argparse.ArgumentParser(description='Pose landmarks and cautious Home Alone state test')
    parser.add_argument('--camera', type=int, default=1)
    parser.add_argument('--model', default='backend/models/pose_landmarker_lite.task')
    parser.add_argument('--show-events', action='store_true', help='Display fall/no-movement test warnings locally; never send alerts')
    args = parser.parse_args()

    import cv2
    from .safety import PoseEstimator, HomeAloneMonitor

    estimator = PoseEstimator(args.model)
    if not estimator.ready:
        raise SystemExit('MediaPipe unavailable: ' + str(estimator.error))
    cap = cv2.VideoCapture(args.camera, cv2.CAP_DSHOW if __import__('os').name == 'nt' else cv2.CAP_ANY)
    if not cap.isOpened():
        estimator.close()
        raise SystemExit(f'Cannot open camera {args.camera}. Stop existing server or try --camera 0.')
    monitor = HomeAloneMonitor()
    last_notice = 'Ready — stand fully visible in frame'
    last_detected = 0
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                print('Camera frame unavailable')
                break
            reading = estimator.read(frame)
            now = time.monotonic()
            if reading:
                last_detected = now
                label = 'POSE: HORIZONTAL (possible fall posture)' if reading.horizontal else 'POSE: UPRIGHT'
                cv2.circle(frame, (int(reading.center_x * frame.shape[1]), int(reading.center_y * frame.shape[0])), 7, (0, 255, 255), -1)
            else:
                label = 'POSE: NOT DETECTED (no fall conclusion)'
            signal = monitor.observe(reading, now) if args.show_events else None
            if signal:
                last_notice = 'TEST ONLY: ' + signal
                print(last_notice)
            cv2.putText(frame, label, (12, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (0, 255, 0) if reading else (0, 160, 255), 2)
            cv2.putText(frame, last_notice[:65], (12, 65), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 2)
            cv2.imshow('VisionSync pose test - q to quit', frame)
            if cv2.waitKey(1) & 0xFF == ord('q'):
                break
    finally:
        cap.release()
        estimator.close()
        cv2.destroyAllWindows()


if __name__ == '__main__':
    main()
