# Local changes to mjpeg_stream 1.0.1

Source: https://github.com/MohammedShamseerPV/MJPEG-Stream/

This directory contains the existing package's Dart source and original MIT
license. It is used as a path dependency so fixes stay inside this project.

The local changes handle closing a viewer while an HTTP request is pending,
cancel previous requests on retry, ignore stale callbacks, report a stream that
ends, and respect the live-indicator setting. An optional HTTP client factory
supports regression tests without a real camera. The existing MJPEG parsing
and visual design are retained.

Do not edit the shared Pub cache to make these changes.
