#!/usr/bin/env python3
"""
download_models.py - Fetch every offline model VisionSync might need.

Run on a PC with good internet, pointing at your USB drive:

    python download_models.py E:\\visionsync-models         (Windows)
    python3 download_models.py /media/you/USB/visionsync-models

Then copy the whole folder to the Jetson:  ~/visionsync/models/

- Safe to run again: finished files are skipped, broken downloads continue.
- A link that fails is reported at the end; the rest still download.
- "--core" downloads only the four files the mirror needs first (about 2.6 GB).
Standard library only (Python 3.6+).
"""
import os
import sys
import time
import urllib.request
import urllib.error

HF = "https://huggingface.co/"
GCS = "https://storage.googleapis.com/mediapipe-models/pose_landmarker/"

# (core?, folder, file name, url, what it is for)
FILES = [
    # --- wake word (Vosk) ---
    (True,  "wake-word", "vosk-model-small-en-us-0.15.zip",
     "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip", "wake word, small (40 MB)"),
    (False, "wake-word", "vosk-model-en-us-0.22-lgraph.zip",
     "https://alphacephei.com/vosk/models/vosk-model-en-us-0.22-lgraph.zip", "wake word, more accurate (128 MB)"),
    # --- speech-to-text (Whisper, whisper.cpp format) ---
    (True,  "whisper-cpp", "ggml-small.en.bin", HF + "ggerganov/whisper.cpp/resolve/main/ggml-small.en.bin", "speech-to-text, accurate (466 MB)"),
    (False, "whisper-cpp", "ggml-base.en.bin", HF + "ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin", "speech-to-text, faster (142 MB)"),
    (False, "whisper-cpp", "ggml-tiny.en.bin", HF + "ggerganov/whisper.cpp/resolve/main/ggml-tiny.en.bin", "speech-to-text, fastest (78 MB)"),
    (False, "whisper-cpp", "ggml-small.bin", HF + "ggerganov/whisper.cpp/resolve/main/ggml-small.bin", "speech-to-text, many languages incl. Urdu (466 MB)"),
    # --- offline chatbot (GGUF for llama.cpp / Ollama) ---
    (True,  "llm", "Llama-3.2-3B-Instruct-Q4_K_M.gguf",
     HF + "bartowski/Llama-3.2-3B-Instruct-GGUF/resolve/main/Llama-3.2-3B-Instruct-Q4_K_M.gguf", "offline chatbot, main (2.0 GB)"),
    (False, "llm", "gemma-2-2b-it-Q4_K_M.gguf",
     HF + "bartowski/gemma-2-2b-it-GGUF/resolve/main/gemma-2-2b-it-Q4_K_M.gguf", "offline chatbot, Gemma alternative (about 1.7 GB)"),
    (False, "llm", "Llama-3.2-1B-Instruct-Q4_K_M.gguf",
     HF + "bartowski/Llama-3.2-1B-Instruct-GGUF/resolve/main/Llama-3.2-1B-Instruct-Q4_K_M.gguf", "offline chatbot, smallest and fastest (about 0.8 GB)"),
    # --- fall detection (MediaPipe pose) ---
    (True,  "pose", "pose_landmarker_full.task", GCS + "pose_landmarker_full/float16/latest/pose_landmarker_full.task", "pose, balanced"),
    (False, "pose", "pose_landmarker_lite.task", GCS + "pose_landmarker_lite/float16/latest/pose_landmarker_lite.task", "pose, fastest"),
    (False, "pose", "pose_landmarker_heavy.task", GCS + "pose_landmarker_heavy/float16/latest/pose_landmarker_heavy.task", "pose, most accurate"),
    # --- offline voice (Piper, nicer than espeak) ---
    (False, "tts-piper", "en_US-lessac-medium.onnx",
     HF + "rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx", "offline voice (about 60 MB)"),
    (False, "tts-piper", "en_US-lessac-medium.onnx.json",
     HF + "rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx.json", "settings for the offline voice"),
]


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024.0


def fetch(url, dest):
    """Downloads to dest + '.part' (continuing if it exists), then renames."""
    part = dest + ".part"
    have = os.path.getsize(part) if os.path.exists(part) else 0
    req = urllib.request.Request(url, headers={"User-Agent": "visionsync-downloader/1.0"})
    if have:
        req.add_header("Range", "bytes=%d-" % have)
    try:
        resp = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as e:
        if e.code == 416 and have:           # the .part file is already complete
            os.replace(part, dest)
            return
        raise
    if have and resp.getcode() != 206:       # server ignored the range: start over
        have = 0
    total = resp.headers.get("Content-Length")
    total = int(total) + have if total else None
    last = 0.0
    with open(part, "ab" if have else "wb") as f:
        while True:
            chunk = resp.read(1024 * 512)
            if not chunk:
                break
            f.write(chunk)
            have += len(chunk)
            if time.time() - last > 0.5:
                last = time.time()
                pct = " (%d%%)" % (have * 100 // total) if total else ""
                sys.stdout.write("\r    %s%s        " % (human(have), pct))
                sys.stdout.flush()
    resp.close()
    if total and have < total:
        raise IOError("connection dropped at %s of %s (run again to continue)" % (human(have), human(total)))
    os.replace(part, dest)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    core_only = "--core" in sys.argv
    if not args:
        print(__doc__)
        return 1
    root = os.path.abspath(args[0])
    todo = [f for f in FILES if f[0] or not core_only]
    failed = []
    for i, (_core, folder, name, url, note) in enumerate(todo, 1):
        d = os.path.join(root, folder)
        os.makedirs(d, exist_ok=True)
        dest = os.path.join(d, name)
        print("[%d/%d] %s  -  %s" % (i, len(todo), name, note))
        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            print("    already here (%s)" % human(os.path.getsize(dest)))
            continue
        ok = False
        for attempt in range(1, 4):
            try:
                fetch(url, dest)
                print("\r    done (%s)            " % human(os.path.getsize(dest)))
                ok = True
                break
            except urllib.error.HTTPError as e:
                print("\r    link answered %s" % e.code)
                if e.code in (401, 403, 404):
                    break                    # retrying will not help
            except Exception as e:
                print("\r    try %d failed: %s" % (attempt, e))
                time.sleep(3)
        if not ok:
            failed.append((name, url))
    print()
    if failed:
        print("NOT downloaded (%d). Open the link in a browser, or send this list to get a new link:" % len(failed))
        for name, url in failed:
            print("  - %s\n      %s" % (name, url))
        return 2
    print("All %d files are in %s" % (len(todo), root))
    return 0


if __name__ == "__main__":
    sys.exit(main())
