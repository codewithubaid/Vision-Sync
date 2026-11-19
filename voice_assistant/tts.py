"""
tts.py - the mirror's voice (SRS UC-2 step 4) and the wake chime (step 1).

Only free voices that run on the Jetson (no paid voice service):
     Piper   (natural, offline; PIPER_MODEL = path to a .onnx voice, kept loaded in memory)
  -> eSpeak  (offline; the same engine pyttsx3 drives on Linux)
  -> pyttsx3 (last resort)
Replies are spoken sentence by sentence: the next sentence is prepared while one plays, and an
answer that is still being written starts as soon as its first sentence is ready. stop() goes
quiet at once (the user talked over Vision). Audio is played from memory; nothing is kept on
disk (NFR-SEC3).
"""
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave

import numpy as np

log = logging.getLogger("tts")
CHIME_RATE = 22050


def clean_for_speech(text):
    text = str(text or "")
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub("[\U0001F000-\U0001FAFF☀-➿️‍⃣]+", "", text)
    text = text.replace("°C", " degrees").replace("°", " degrees").replace("%", " percent").replace("&", " and ")
    text = re.sub(r"[*_`#>|]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _tones(freqs, seconds=0.09, rate=CHIME_RATE):
    parts = []
    for f in freqs:
        t = np.arange(int(rate * seconds)) / float(rate)
        fade = np.minimum(1.0, np.minimum(t, seconds - t) / 0.012)
        parts.append(0.35 * np.sin(2 * np.pi * f * t) * fade)
    return (np.concatenate(parts) * 32767).astype("<i2").tobytes()


CHIME = _tones((880, 1320))


def _sentences(text):
    """Splits a reply into sentences, so the first one is spoken while the next is being prepared."""
    parts = [p.strip() for p in re.split(r"(?<=[.!?;:])\s+", text) if p.strip()]
    out = []
    for p in parts:
        if out and len(out[-1]) < 25:
            out[-1] += " " + p                    # keep very short bits together ("Sure. It is ...")
        else:
            out.append(p)
    return out or ([text] if text else [])


def _synth(voice, text):
    """Raw 16-bit audio from a loaded Piper voice (piper-tts 1.2 and 1.3+)."""
    if hasattr(voice, "synthesize_stream_raw"):
        return b"".join(voice.synthesize_stream_raw(text)), int(voice.config.sample_rate)
    pcm, rate = [], 22050
    for chunk in voice.synthesize(text):
        pcm.append(chunk.audio_int16_bytes)
        rate = chunk.sample_rate
    return b"".join(pcm), int(rate)


class Speaker(object):
    def __init__(self, online=lambda: True):
        self.online = online
        self.piper_model = os.path.expanduser(os.environ.get("PIPER_MODEL", "").strip())
        venv_piper = os.path.join(os.path.dirname(sys.executable), "piper")
        self.piper = shutil.which("piper") or (venv_piper if os.path.isfile(venv_piper) else None)
        self.espeak = shutil.which("espeak-ng") or shutil.which("espeak")
        self.aplay = shutil.which("aplay")
        self.paplay = shutil.which("paplay")
        self.device = os.environ.get("SPEAKER_DEVICE", "").strip()     # aplay -D name; empty = system default
        self.lock = threading.Lock()
        self.engine = ""
        self.audio_started = 0.0
        self.playing = False                      # True while sound comes out (barge-in listens around it)
        self._halt = threading.Event()            # set by stop(): the user started talking
        self._procs = set()
        self._plock = threading.Lock()
        self._vlock = threading.Lock()
        self._pvoice = None                       # the Piper voice, loaded once and kept in memory
        self._pvoice_failed = False
        if self.piper_model and os.path.isfile(self.piper_model):
            threading.Thread(target=self._piper_voice, daemon=True).start()   # ready before the first answer

    # ------------------------------------------------------------ stopping (barge-in)
    def prepare(self):
        """Clears an earlier stop before a reply that the user may talk over."""
        self._halt.clear()

    def stop(self):
        """Goes quiet at once: the user started talking."""
        self._halt.set()
        with self._plock:
            procs = list(self._procs)
        for p in procs:
            try:
                p.terminate()
            except Exception:
                pass

    def _run(self, cmd, data=None, timeout=180, playback=False, interruptible=True):
        """Runs a command that stop() can cut short. True when it finished normally."""
        if interruptible and self._halt.is_set():
            return False
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if interruptible:
            with self._plock:
                self._procs.add(p)
            if self._halt.is_set():
                p.terminate()
        if playback:
            self.playing = True
        try:
            p.communicate(input=data, timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            p.communicate()
        except OSError:
            pass
        finally:
            if playback:
                self.playing = False
            with self._plock:
                self._procs.discard(p)
        return p.returncode == 0 and not (interruptible and self._halt.is_set())

    # ------------------------------------------------------------ playback
    def _player_cmd(self, rate):
        if self.aplay:
            cmd = [self.aplay, "-q", "-t", "raw", "-f", "S16_LE", "-r", str(rate), "-c", "1"]
            if self.device:
                cmd[1:1] = ["-D", self.device]
            return cmd
        if self.paplay:
            return [self.paplay, "--raw", "--format=s16le", "--rate=%d" % rate, "--channels=1"]
        return None

    def play(self, pcm, rate):
        if self._halt.is_set():
            return False
        if not self.audio_started:
            self.audio_started = time.time()
        cmd = self._player_cmd(rate)
        if cmd is not None:
            return self._run(cmd, pcm, timeout=180, playback=True)
        import sounddevice as sd
        self.playing = True
        try:
            sd.play(np.frombuffer(pcm, dtype="<i2"), rate)
            while sd.get_stream().active and not self._halt.is_set():
                time.sleep(0.05)
            sd.stop()
        finally:
            self.playing = False
        return not self._halt.is_set()

    def chime(self):
        try:
            cmd = self._player_cmd(CHIME_RATE)
            if cmd is not None:
                self._run(cmd, CHIME, timeout=10, interruptible=False)
            else:
                import sounddevice as sd
                sd.play(np.frombuffer(CHIME, dtype="<i2"), CHIME_RATE)
                sd.wait()
        except Exception as e:
            log.warning("Chime failed: %s", type(e).__name__)

    # ------------------------------------------------------------ speaking
    def speak(self, text, fresh=True):
        text = clean_for_speech(text)
        if not text:
            return False
        return self.speak_stream([text], fresh=fresh)

    def speak_stream(self, parts, fresh=True):
        """Speaks the parts in order, as they arrive (an answer may still be being written).
        Piper prepares the next sentence while the current one plays; eSpeak if Piper is missing."""
        with self.lock:
            if fresh:
                self._halt.clear()
            self.audio_started = 0.0
            ready = queue.Queue(maxsize=2)
            use_piper = self._piper_ready()

            def prepare():
                try:
                    for part in parts:
                        cleaned = clean_for_speech(part)
                        for sentence in (_sentences(cleaned) if cleaned else []):
                            if self._halt.is_set():
                                return
                            audio = None
                            if use_piper:
                                try:
                                    audio = self._piper_pcm(sentence)
                                except Exception as e:
                                    log.warning("Piper failed: %s", type(e).__name__)
                            ready.put((sentence, audio))
                finally:
                    ready.put(None)

            threading.Thread(target=prepare, daemon=True).start()
            spoke = False
            while True:
                item = ready.get()
                if item is None:
                    break
                if self._halt.is_set():
                    continue                      # let the preparer finish quietly
                sentence, audio = item
                if audio is not None and len(audio[0]) >= 100:
                    if self.play(*audio):
                        spoke, self.engine = True, "piper"
                    continue
                for name, fn in (("espeak", self._espeak), ("pyttsx3", self._pyttsx3)):
                    if self._halt.is_set():
                        break
                    try:
                        if fn(sentence):
                            spoke, self.engine = True, name
                            break
                    except Exception as e:
                        log.warning("Voice %s failed: %s", name, type(e).__name__)
            if not spoke and not self._halt.is_set():
                log.error("No voice worked. Install them with: sudo apt install espeak-ng alsa-utils")
            return spoke or self._halt.is_set()

    # ------------------------------------------------------------ engines (all on the Jetson)
    def _piper_voice(self):
        """The Piper voice kept in memory (loaded once), or None to use the piper program."""
        with self._vlock:
            if self._pvoice is None and not self._pvoice_failed:
                try:
                    from piper import PiperVoice
                    self._pvoice = PiperVoice.load(self.piper_model)
                except Exception as e:
                    self._pvoice_failed = True
                    log.info("Piper voice in memory not available (%s): using the piper program",
                             type(e).__name__)
            return self._pvoice

    def _piper_ready(self):
        return bool(self.piper_model and os.path.isfile(self.piper_model)
                    and (self.piper or not self._pvoice_failed))

    def _piper_pcm(self, sentence):
        voice = self._piper_voice()
        if voice is not None:
            try:
                return _synth(voice, sentence)
            except Exception as e:
                log.warning("Piper voice failed: %s", type(e).__name__)
        if not self.piper:
            return None
        fd, path = tempfile.mkstemp(suffix=".wav", dir="/dev/shm" if os.path.isdir("/dev/shm") else None)
        os.close(fd)
        try:
            if not self._run([self.piper, "-m", self.piper_model, "-f", path], sentence.encode("utf-8"),
                             timeout=60):
                return None
            if os.path.getsize(path) < 100:
                return None
            with wave.open(path, "rb") as w:
                return w.readframes(w.getnframes()), w.getframerate()
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def _espeak(self, text):
        if not self.espeak:
            return False
        if not self.audio_started:
            self.audio_started = time.time()
        return self._run([self.espeak, "-v", os.environ.get("ESPEAK_VOICE", "en-us"),
                          "-s", os.environ.get("ESPEAK_SPEED", "165"), "--stdin"],
                         text.encode("utf-8"), timeout=120, playback=True)

    def _pyttsx3(self, text):
        import pyttsx3
        engine = pyttsx3.init()
        engine.setProperty("rate", 165)
        if not self.audio_started:
            self.audio_started = time.time()
        engine.say(text)
        engine.runAndWait()
        try:
            engine.stop()
        except Exception:
            pass
        return True
