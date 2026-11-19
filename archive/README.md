# Archive: not used on the mirror

Kept so nothing from the team's work is lost. Do not run these on the Jetson
next to `camera_backend/`: only one program can use the webcam.

- `partner_camera_servers_not_used_on_mirror/`: the two servers that came inside
  the integrated Flutter zip. `motion_stream_server.py` there is the port-5000
  stream with no password. `backend/` is the FastAPI server with Supabase
  sign-in, incident history and Firebase push; the phone app never calls it.
  Its pose code (`backend/safety.py`) is what the merged server's
  `pose_fall.py` is based on.
- `old_unused_helpers/`: `call_dispatcher.py` and `pose_detector.py` from the
  first camera backend. Nothing imported them.
