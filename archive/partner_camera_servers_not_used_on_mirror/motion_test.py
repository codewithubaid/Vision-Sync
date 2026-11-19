import cv2


def main():
    camera = cv2.VideoCapture(1)
    previous_frame = None
    window_name = "Motion Test - press Q to close"

    try:
        if not camera.isOpened():
            print("Camera nahi khul raha. Koi doosra camera program band karo.")
            return

        while True:
            success, frame = camera.read()
            if not success or frame is None:
                print("Camera se image nahi mil rahi.")
                break

            frame = cv2.resize(frame, (640, 480))

            # Black-and-white image; blur reduces small image noise.
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            gray = cv2.GaussianBlur(gray, (21, 21), 0)

            motion_detected = False
            status = "Starting..."

            if previous_frame is not None:
                # Find pixels that changed between two camera images.
                difference = cv2.absdiff(previous_frame, gray)
                _, mask = cv2.threshold(
                    difference, 25, 255, cv2.THRESH_BINARY
                )
                mask = cv2.dilate(mask, None, iterations=2)

                contours, _ = cv2.findContours(
                    mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
                )

                for contour in contours:
                    # Ignore very small changes.
                    if cv2.contourArea(contour) < 1000:
                        continue

                    motion_detected = True
                    x, y, width, height = cv2.boundingRect(contour)
                    cv2.rectangle(
                        frame, (x, y), (x + width, y + height),
                        (0, 0, 255), 2
                    )

                status = (
                    "Motion detected" if motion_detected else "No motion"
                )

            previous_frame = gray

            color = (0, 0, 255) if motion_detected else (0, 255, 0)
            cv2.putText(
                frame, status, (15, 35),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2
            )
            cv2.imshow(window_name, frame)

            if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                break

            try:
                if cv2.getWindowProperty(
                    window_name, cv2.WND_PROP_VISIBLE
                ) < 1:
                    break
            except cv2.error:
                break

    except KeyboardInterrupt:
        pass
    finally:
        camera.release()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()