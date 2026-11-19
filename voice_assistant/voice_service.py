"""
voice_service.py - hands-free voice assistant of the mirror (SRS UC-2, UC-4).

  standby    Vosk listens for "Hey Vision" said near the mirror (voices across the room are ignored)
  wake       chime; the mirror shows "Listening"
  record     until END_SILENCE_SECONDS of silence (default 0.7 s)
  text       OpenAI speech-to-text when online (STT_PROVIDER), else faster-whisper here
  answer     ai_router.ask_ai: mirror skills, tasks, reminders, OpenAI or Gemini, Qwen3 offline
  speak      tts.py, sentence by sentence while the answer is still being written; talking
             over Vision stops it at once (BARGE_IN), and it keeps listening to that person
  follow-up  listens again for FOLLOWUP_SECONDS without the wake word, then standby

Audio stays in memory and the conversation is forgotten at the end of each
session (NFR-SEC3). Errors are logged and the loop starts again (NFR-Q5).
api_server.py starts it. Alone:  python voice_service.py
"""
import csv
import json
import logging
import os
import queue
import re
import subprocess
import shutil
import threading
import time
from collections import deque
from math import gcd

import numpy as np

import mirror_link
import ai_router
import mirror_skills
from tts import Speaker

log = logging.getLogger("voice")
HERE = os.path.dirname(os.path.abspath(__file__))


def _num(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


SAMPLE_RATE = 16000
CHUNK_SECONDS = 0.1
WAKE_WORDS = [w.strip().lower() for w in os.environ.get("WAKE_WORDS", "hey vision").split(",") if w.strip()]
VOSK_MODEL_PATH = os.path.expanduser(os.environ.get("VOSK_MODEL_PATH", "").strip() or os.path.join(HERE, "vosk-model"))
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "").strip() or "base.en"
WHISPER_BEAM = int(_num("WHISPER_BEAM", 1))
WHISPER_THREADS = int(_num("WHISPER_THREADS", min(6, os.cpu_count() or 4)))
END_SILENCE = _num("END_SILENCE_SECONDS", 0.7)
FIRST_WAIT = _num("LISTEN_SECONDS", 8)
FOLLOWUP_WAIT = _num("FOLLOWUP_SECONDS", 8)
MAX_UTTERANCE = _num("MAX_UTTERANCE_SECONDS", 15)
MIN_LOGPROB = _num("STT_MIN_LOGPROB", -1.2)
SPEECH_THRESHOLD = _num("SPEECH_THRESHOLD", 0)          # 0 = adapt to the room
_dev = os.environ.get("MIC_DEVICE", "").strip()
MIC_DEVICE = int(_dev) if _dev.isdigit() else (_dev or None)
TIMINGS_FILE = os.path.expanduser(os.environ.get("VOICE_TIMINGS_FILE", "~/.cache/visionsync/voice_timings.csv"))

# Speech to text online: quicker than the Jetson's processor and better with accents.
# auto = OpenAI when the mirror is online and OPENAI_API_KEY is set; local = always Whisper here.
STT_PROVIDER = os.environ.get("STT_PROVIDER", "auto").strip().lower()
STT_MODEL = os.environ.get("STT_MODEL", "").strip() or "gpt-transcribe"
STT_CLOUD_TIMEOUT = _num("STT_CLOUD_TIMEOUT", 4)
STT_PROMPT = ("A short question to a smart mirror named Vision. It may mention Wi-Fi, Security Mode, "
              "Home Alone Mode, tasks, reminders, the weather or the calendar.")
# Barge-in: talking while Vision speaks makes it stop at once, like a person would.
# on = your voice (or the words Vision / stop / wait); words = only those words; off = never.
BARGE_IN = os.environ.get("BARGE_IN", "on").strip().lower()
BARGE_IN_RATIO = _num("BARGE_IN_RATIO", 3.0)
# Voice focus: only the person near the mirror counts. "Hey Vision" must be WAKE_NEAR times louder
# than the room; their question must reach VOICE_FOCUS of their own "Hey Vision" loudness.
WAKE_NEAR = _num("WAKE_NEAR", 1.5)
VOICE_FOCUS = _num("VOICE_FOCUS", 0.25)
_stt_off_until = [0.0]

NOT_CAUGHT = "I did not catch that. Please repeat your question."      # SRS UC-2 alternate 2
GOODBYE = "Okay. Say Hey Vision when you need me."
HALLUCINATIONS = {"you", "thanks for watching", "thank you for watching", "subtitles by the amara org community"}

_stop = threading.Event()
_lock = threading.Lock()
_thread = None
_callback = None
_announce = queue.Queue()
_status = {"enabled": False, "stage": "off", "error": ""}
_speaker = None


# ------------------------------------------------------------------ events for api_server / web page
def emit(kind, **payload):
    cb = _callback
    if cb:
        try:
            payload["type"] = kind
            cb(payload)
        except Exception as e:
            log.warning("Event callback error: %s", e)


def _set_stage(name, message=""):
    _status["stage"] = name
    emit("status", status=name, message=message)


# ------------------------------------------------------------------ commands
def normalize_command(text):
    text = str(text or "").lower().replace("’", "'").replace("'", "")
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


_FAREWELL = {"bye", "goodbye", "end", "finish", "done", "sleep", "stop", "cancel", "nevermind", "nothing"}
_ALLOWED = _FAREWELL | {"thank", "thanks", "you", "very", "much", "okay", "ok", "alright", "well", "then", "vision",
                        "hey", "please", "thats", "that", "is", "all", "im", "i", "am", "the", "chat", "conversation",
                        "see", "later", "chatting", "go", "to", "now", "no", "for", "it"}


def is_end_chat(text):
    """Farewells that end the conversation ("bye", "that's all, thanks", "never mind")."""
    words = normalize_command(text)
    words = re.sub(r"\bgood\s+bye\b", "goodbye", words)
    words = re.sub(r"\bnever\s+mind\b", "nevermind", words).split()
    return bool(words) and any(w in _FAREWELL for w in words) and all(w in _ALLOWED for w in words)


def is_exit_command(text):
    return normalize_command(text) in {"shutdown vision", "shut down vision", "exit assistant", "close assistant",
                                       "turn off vision", "quit vision", "please shut down vision"}


# ------------------------------------------------------------------ microphone
def _rms(x):
    return float(np.sqrt(np.mean(np.square(x)) + 1e-12))


class Mic(object):
    """USB webcam microphone. Delivers 0.1 s blocks of 16 kHz mono float audio."""

    def __init__(self):
        self.stream = None
        self.rate = SAMPLE_RATE
        self.noise = 0.003
        self.voice = 0.0                          # loudness of the person talking to Vision
        self.device = None

    def open(self):
        import sounddevice as sd
        errors, tried = [], []
        # The chosen mic first; then the sound server, i.e. the mic picked in Settings > Sound > Input.
        for device in (MIC_DEVICE, "pipewire", "default", None):
            if device in tried:
                continue
            tried.append(device)
            try:
                native = int(sd.query_devices(device, "input")["default_samplerate"])
            except Exception as e:
                errors.append("%s: %s" % (device, e))
                continue
            for rate in (SAMPLE_RATE, native, 48000, 44100):
                try:
                    s = sd.InputStream(samplerate=rate, channels=1, dtype="float32",
                                       blocksize=int(rate * CHUNK_SECONDS), device=device)
                    s.start()
                    self.stream, self.rate, self.device = s, rate, device
                    if device != MIC_DEVICE:
                        log.warning("Microphone %r not usable; using %r", MIC_DEVICE, device)
                    return
                except Exception as e:
                    errors.append("%s %d Hz: %s" % (device, rate, e))
        raise RuntimeError("Microphone not available (%s). Plug it in and choose it in "
                           "Settings > Sound > Input." % (errors[-1] if errors else "no input device"))

    def read(self):
        data, _overflow = self.stream.read(int(self.rate * CHUNK_SECONDS))
        x = np.asarray(data, dtype=np.float32).reshape(-1)
        if self.rate != SAMPLE_RATE:
            from scipy.signal import resample_poly
            g = gcd(SAMPLE_RATE, int(self.rate))
            x = resample_poly(x, SAMPLE_RATE // g, int(self.rate) // g).astype(np.float32)
        return x

    def track(self, x):
        """Follows the room's background noise so speech is found in quiet and loud rooms (NFR-Q5)."""
        level = _rms(x)
        if level < self.noise * 2.5:
            self.noise = 0.95 * self.noise + 0.05 * level
        else:
            self.noise = 0.998 * self.noise + 0.002 * level

    def threshold(self):
        return SPEECH_THRESHOLD or max(0.004, min(0.05, self.noise * 3.0))

    def close(self):
        s, self.stream = self.stream, None
        if s is not None:
            try:
                s.stop()
                s.close()
            except Exception:
                pass


def record(mic, wait_seconds, ignore=0.0, preroll=None):
    """Records one question from the person near the mirror. Returns 16 kHz float32 audio, or None
    when nobody spoke. Background voices are quieter than the speaker's own voice, so the question
    ends when the speaker stops, even in a noisy hall.
    preroll: audio heard just before (the user's first words when they talked over Vision)."""
    thr = mic.threshold()
    start_thr = max(thr, mic.voice * VOICE_FOCUS)           # the speaker, not the crowd
    preroll = list(preroll or [])
    pre = deque(preroll, maxlen=6 + len(preroll))           # keeps the moment just before the voice started
    frames, started, speech = [], False, []
    run = silence = waited = length = 0.0
    while not _stop.is_set():
        x = mic.read()
        level = _rms(x)
        if not started:
            pre.append(x)
            waited += CHUNK_SECONDS
            if waited > ignore and level >= start_thr:      # the chime is not speech
                run += CHUNK_SECONDS
                speech.append(level)
                if run >= 0.2:
                    started = True
                    frames.extend(pre)
                    emit("status", status="recording", message="Recording your voice.")
                continue
            run, speech = 0.0, []
            if waited >= wait_seconds + ignore:
                return None
            continue
        frames.append(x)
        length += CHUNK_SECONDS
        if level >= start_thr:
            speech.append(level)
        voice = float(np.percentile(speech, 60)) if speech else start_thr
        end_thr = max(thr * 0.7, voice * 0.3)               # well below their voice: they stopped talking
        silence = silence + CHUNK_SECONDS if level < end_thr else 0.0
        if silence >= END_SILENCE or length >= MAX_UTTERANCE:
            break
    if not started or _stop.is_set():
        return None
    if speech:
        heard = float(np.percentile(speech, 60))
        mic.voice = heard if not mic.voice else 0.7 * mic.voice + 0.3 * heard     # follow the speaker
    return np.concatenate(frames).astype(np.float32)


# ------------------------------------------------------------------ models
def load_models():
    from vosk import Model, SetLogLevel
    SetLogLevel(-1)
    if not os.path.isdir(VOSK_MODEL_PATH):
        raise RuntimeError("Wake-word model not found at %s. Run setup_voice.sh." % VOSK_MODEL_PATH)
    vosk = Model(VOSK_MODEL_PATH)
    from faster_whisper import WhisperModel
    whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8", cpu_threads=WHISPER_THREADS,
                           download_root=os.path.join(HERE, "models", "whisper"))
    segments, _ = whisper.transcribe(np.zeros(SAMPLE_RATE // 2, dtype=np.float32), language="en", beam_size=1)
    list(segments)                              # warm-up: the first real question is not slow
    return vosk, whisper


def _warm_local_model():
    """Loads Qwen3 into memory so an internet drop is answered fast (UC-4)."""
    try:
        import requests
        requests.post(ai_router.OLLAMA_URL.replace("/api/chat", "/api/generate"),
                      json={"model": ai_router.FAST_MODEL, "keep_alive": ai_router.LOCAL_KEEP_ALIVE}, timeout=180)
    except Exception as e:
        log.warning("Local model not ready yet: %s", type(e).__name__)


def transcribe(whisper, audio):
    segments, _info = whisper.transcribe(
        audio, language="en", beam_size=WHISPER_BEAM, temperature=0.0, vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500}, condition_on_previous_text=False,
        without_timestamps=True, initial_prompt="Vision, Wi-Fi, Security Mode, Home Alone Mode.")
    segments = list(segments)
    text = " ".join(s.text.strip() for s in segments if s.text.strip()).strip()
    if not text:
        return ""
    if is_end_chat(text) or is_exit_command(text):
        return text
    logprobs = [s.avg_logprob for s in segments]
    no_speech = [s.no_speech_prob for s in segments]
    if logprobs and sum(logprobs) / len(logprobs) < MIN_LOGPROB:
        return ""                                # confidence below threshold (UC-2 alternate 2)
    if no_speech and sum(no_speech) / len(no_speech) > 0.7:
        return ""
    if normalize_command(text) in HALLUCINATIONS:
        return ""
    return text


# ------------------------------------------------------------------ the speaker's voice, not the room
def _said(heard, phrases):
    """True when one of the phrases is among the recognised words (whole words only)."""
    h = " %s " % normalize_command(heard)
    return any(" %s " % normalize_command(p) in h for p in phrases)


_WAKE_PREFIX = re.compile(r"^\W*(?:(?:hey|hi|okay|ok)[\s,]+)?vision\b[\s,.!?:;-]*", re.I)
_FIXES = [(re.compile(r"\bhome\s+(?:along|a\s*lone|lone)\b", re.I), "home alone"),
          (re.compile(r"\bsecurity\s+mood\b", re.I), "security mode"),
          (re.compile(r"\b(?:why|wi)\s*-?\s*fi\b", re.I), "Wi-Fi")]


def _focus_floor(mic):
    """Loudness that counts as the speaker's voice (the room and far voices stay below it)."""
    thr = mic.threshold() if hasattr(mic, "threshold") else 0.004
    return max(thr, getattr(mic, "voice", 0.0) * VOICE_FOCUS)


def _fix_transcript(text):
    """Drops "Hey Vision" said again at the start and fixes words speech-to-text often gets wrong."""
    text = str(text or "").strip()
    rest = _WAKE_PREFIX.sub("", text).strip()
    if rest:
        text = rest
    for pattern, good in _FIXES:
        text = pattern.sub(good, text)
    return text


def clean_audio(audio, floor):
    """Keeps only the speaker's part of the recording: trims the room before and after, removes
    rumble and evens out the volume. Quicker to send, and fewer stray words from the background."""
    hop = SAMPLE_RATE // 50                                  # 20 ms
    n = len(audio) // hop
    if n < 5:
        return audio
    levels = np.sqrt(np.mean(np.square(audio[:n * hop].reshape(n, hop)), axis=1) + 1e-12)
    loud = np.flatnonzero(levels >= floor)
    if loud.size:
        audio = audio[max(0, (loud[0] - 12) * hop):min(len(audio), (loud[-1] + 16) * hop)]
    try:
        from scipy.signal import butter, sosfilt
        audio = sosfilt(butter(2, 90, "highpass", fs=SAMPLE_RATE, output="sos"), audio)
    except Exception:
        pass
    audio = np.asarray(audio, dtype=np.float32)
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak > 1e-4:
        audio = (audio * min(0.9 / peak, 20.0)).astype(np.float32)
    return audio


class SentenceStream(object):
    """The answer arrives a few words at a time. The voice gets each sentence as soon as it is
    complete, so Vision starts talking before the whole answer is written."""

    _END = re.compile(r"[.!?](?=\s)|\n")

    def __init__(self):
        self.q = queue.Queue()
        self.buf, self.text, self.sent, self.closed = "", "", False, False
        self.lock = threading.Lock()

    def feed(self, delta):
        with self.lock:
            if self.closed or not delta:
                return
            self.text += delta
            self.buf = re.sub(r"<think>.*?</think>", "", self.buf + delta, flags=re.S | re.I)
            if "<think>" in self.buf.lower():
                return                                    # hidden reasoning: wait for its end
            while True:
                cut = next((m.end() for m in self._END.finditer(self.buf)
                            if m.end() >= (12 if self.sent else 20)), None)
                if cut is None and not self.sent and len(self.buf) > 90:
                    comma = self.buf.find(", ", 40)       # a long first sentence: start at a comma
                    cut = comma + 1 if comma > 0 else None
                if cut is None:
                    return
                part, self.buf = self.buf[:cut].strip(), self.buf[cut:]
                if part:
                    self.sent = True
                    self.q.put(part)

    def finish(self, reply=""):
        """The answer is complete. reply = the final text, spoken whole if nothing was streamed."""
        with self.lock:
            if self.closed:
                return
            self.closed = True
            rest = re.sub(r"<think>.*?(?:</think>|$)", "", self.buf, flags=re.S | re.I).strip()
            if not self.sent and reply:
                self.q.put(reply)
            elif rest:
                self.q.put(rest)
            self.q.put(None)

    def close(self):
        """Interrupted: nothing more will be spoken."""
        with self.lock:
            if not self.closed:
                self.closed = True
                self.q.put(None)

    def __iter__(self):
        while True:
            part = self.q.get()
            if part is None:
                return
            yield part


# ------------------------------------------------------------------ speech to text online
def _wav_bytes(audio):
    """The question as a WAV file in memory (never written to disk, NFR-SEC3)."""
    import io
    import wave
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


_OPUS = {"args": None, "checked": False, "why": ""}
_opus_lock = threading.Lock()


def _find_opus():
    """The ffmpeg command that makes Opus on this computer: libopus, else ffmpeg's own Opus encoder.
    None if neither works here: then the question is sent as WAV, which works too."""
    ff = shutil.which("ffmpeg")
    if not ff:
        _OPUS["why"] = "ffmpeg is not installed"
        return None
    silence = np.zeros(SAMPLE_RATE // 2, dtype="<i2").tobytes()
    why = []
    for codec in (["-c:a", "libopus", "-b:a", "32k", "-application", "voip"],
                  ["-strict", "-2", "-c:a", "opus", "-ar", "48000", "-b:a", "32k"]):
        name = codec[codec.index("-c:a") + 1]
        args = [ff, "-hide_banner", "-loglevel", "error", "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1",
                "-i", "pipe:0"] + codec + ["-f", "ogg", "pipe:1"]
        try:
            r = subprocess.run(args, input=silence, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        except Exception as exc:
            why.append("%s: %s" % (name, type(exc).__name__))
            continue
        if r.returncode == 0 and r.stdout.startswith(b"OggS"):
            return args
        lines = (r.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        why.append("%s: %s" % (name, (lines[-1] if lines else "exit %s" % r.returncode)[:120]))
    _OPUS["why"] = "; ".join(why)
    log.warning("ffmpeg cannot make Opus here (%s): questions are sent as WAV", _OPUS["why"])
    return None


def _check_opus(release=False):
    try:
        if not _OPUS["checked"]:
            _OPUS["args"] = _find_opus()
            _OPUS["checked"] = True
    finally:
        if release:
            _opus_lock.release()


def _opus_args(wait=False):
    """The Opus command, found once. wait=False never makes a question wait: the check runs in the
    background and that question goes as WAV."""
    if _OPUS["checked"]:
        return _OPUS["args"]
    if wait:
        with _opus_lock:
            _check_opus()
        return _OPUS["args"]
    if _opus_lock.acquire(blocking=False):
        threading.Thread(target=_check_opus, args=(True,), daemon=True, name="VisionOpusCheck").start()
    return None


def _ogg_bytes(audio):
    """The question as Opus audio (the codec of voice calls): about 8 times smaller than WAV, so it
    is sent much quicker on a slow connection. Made with ffmpeg in memory; None if not possible here."""
    args = _opus_args()
    if not args:
        return None
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    try:
        r = subprocess.run(args, input=pcm, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5)
    except Exception:
        return None
    return r.stdout if r.returncode == 0 and r.stdout.startswith(b"OggS") else None


_STT_OGG = [True]                                 # switched off if OpenAI ever refuses Opus


def _upload(audio):
    """The file to send: small Opus when possible, WAV otherwise. Never written to disk (NFR-SEC3)."""
    if _STT_OGG[0]:
        ogg = _ogg_bytes(audio)
        if ogg:
            return ("question.ogg", ogg, "audio/ogg")
    return ("question.wav", _wav_bytes(audio), "audio/wav")


def transcribe_cloud(audio):
    """OpenAI speech-to-text: quicker than the Jetson's processor and better with accents."""
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    for _ in range(2):
        upload = _upload(audio)
        r = mirror_link.http().post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": "Bearer " + key},
            files={"file": upload},
            data={"model": STT_MODEL, "response_format": "json", "prompt": STT_PROMPT},
            timeout=(3, STT_CLOUD_TIMEOUT))
        if r.status_code == 400 and upload[2] == "audio/ogg":
            _STT_OGG[0] = False                   # this model wants WAV: use WAV from now on
            continue
        break
    if r.status_code != 200:
        raise RuntimeError("speech-to-text HTTP %s" % r.status_code)
    text = str((r.json() or {}).get("text") or "").strip()
    words = normalize_command(text)
    if not words or words in HALLUCINATIONS:
        return ""
    if len(words.split()) >= 5 and words in normalize_command(STT_PROMPT):
        return ""                                 # it repeated its hint: nothing was really said
    return text


def _cloud_stt_ok(vs=None):
    if STT_PROVIDER == "local" or not os.environ.get("OPENAI_API_KEY", "").strip():
        return False
    if time.time() < _stt_off_until[0]:
        return False
    try:
        up = vs.online() if vs is not None else mirror_link.online()
    except Exception:
        up = None
    return up is not False


def speech_to_text(whisper, audio, vs=None):
    """Returns (text, "cloud" or "local"). Online: OpenAI. Offline, or when it fails: Whisper on the Jetson."""
    if _cloud_stt_ok(vs):
        try:
            return _fix_transcript(transcribe_cloud(audio)), "cloud"
        except Exception as e:
            log.warning("Online speech-to-text failed (%s): Whisper on the mirror for a minute", type(e).__name__)
            _stt_off_until[0] = time.time() + 60
    return _fix_transcript(transcribe(whisper, audio)), "local"


# ------------------------------------------------------------------ barge-in: talk over Vision to stop it
class BargeIn(object):
    """Notices the user talking while Vision speaks: the microphone gets clearly louder (BARGE_IN_RATIO
    times) than the echo of Vision's own voice a moment earlier, for 0.3 s."""

    def __init__(self, threshold, ratio=None, near=0.0):
        self.thr = threshold
        self.ratio = ratio or BARGE_IN_RATIO
        self.near = near                          # how loud the speaker is close to the mic
        self.levels = deque(maxlen=15)            # the last 1.5 s
        self.run = 0.0
        self.play_since = None
        self.last_play = -1e9

    def limit(self, playing, now, ratio=None):
        echo = max(list(self.levels)[:-3] or [0.0])        # 0.3 to 1.5 s ago, before the user spoke
        if playing or now - self.last_play < 0.4:           # Vision's voice (or its echo) is in the room
            return max(self.thr * 2.0, echo * (ratio or self.ratio), self.near)
        return max(self.thr * 1.5, self.near)                # only someone close to the mirror counts

    def feed(self, level, playing, now):
        if playing:
            self.last_play = now
            if self.play_since is None:
                self.play_since = now
        else:
            self.play_since = None
        limit = self.limit(playing, now)
        self.levels.append(level)
        if playing and now - self.play_since < 0.6:         # its voice just started: learn the echo first
            self.run = 0.0
            return False
        self.run = self.run + CHUNK_SECONDS if level >= limit else 0.0
        return self.run >= 0.29


class _BargeWords(object):
    """Hears "Hey Vision", "stop" or "wait" while Vision speaks (not when Vision itself says them)."""
    PHRASES = ("hey vision", "stop", "wait")

    def __init__(self, model):
        from vosk import KaldiRecognizer
        self.rec = KaldiRecognizer(model, SAMPLE_RATE, json.dumps(sorted(self.PHRASES) + ["[unk]"]))

    def feed(self, x, spoken=""):
        pcm = (np.clip(x, -1.0, 1.0) * 32767).astype("<i2").tobytes()
        if self.rec.AcceptWaveform(pcm):
            heard = json.loads(self.rec.Result()).get("text", "")
        else:
            heard = json.loads(self.rec.PartialResult()).get("partial", "")
        own = normalize_command(spoken)
        return any(_said(heard, [p]) and not _said(own, [p]) for p in self.PHRASES)


def speak_and_listen(mic, speaker, source, vosk_model=None, on_tick=None):
    """Speaks the reply (a text, or a SentenceStream still being written) and keeps listening.
    If the person near the mirror talks over it, Vision goes quiet at once, stops writing the answer
    and returns (their first words as audio, whether Vision had started talking).
    Otherwise returns (None, True)."""
    stream = source if isinstance(source, SentenceStream) else None
    parts = stream if stream is not None else [source]
    tick = on_tick or (lambda: None)
    if BARGE_IN == "off" or not hasattr(speaker, "stop"):
        if hasattr(speaker, "speak_stream"):
            speaker.speak_stream(parts)
        else:
            speaker.speak(" ".join(parts))
        tick()
        return None, True
    done = threading.Event()
    speaker.prepare()

    def talk():
        try:
            speaker.speak_stream(parts, fresh=False)
        except Exception as e:
            log.warning("Speaking failed: %s", type(e).__name__)
        finally:
            done.set()

    threading.Thread(target=talk, daemon=True).start()
    try:
        if mic.stream is None:
            mic.open()
    except Exception as e:
        log.warning("No barge-in this time (microphone: %s)", type(e).__name__)
        while not done.wait(0.1):
            tick()
        return None, True
    detector = BargeIn(mic.threshold(), near=getattr(mic, "voice", 0.0) * 0.5)
    try:
        words = _BargeWords(vosk_model) if vosk_model is not None else None
    except Exception:
        words = None
    recent = deque(maxlen=5)                      # the last 0.5 s: the person's first words
    while not done.is_set() and not _stop.is_set():
        tick()
        x = mic.read()
        recent.append(x)
        now, level = time.time(), _rms(x)
        playing = bool(getattr(speaker, "playing", False))
        loud = detector.feed(level, playing, now) and BARGE_IN == "on"
        said = (words is not None and words.feed(x, stream.text if stream is not None else source)
                and level >= detector.limit(playing, now, ratio=1.5))
        if loud or said:
            talked = bool(getattr(speaker, "audio_started", 0.0))
            speaker.stop()
            ai_router.cancel()
            if stream is not None:
                stream.close()
            done.wait(2.0)
            emit("status", status="interrupted", message="Listening.")
            return list(recent), talked
    done.wait(5.0)
    tick()
    return None, True


# ------------------------------------------------------------------ standby and conversation
def _new_wake_recognizer(vosk_model):
    from vosk import KaldiRecognizer
    return KaldiRecognizer(vosk_model, SAMPLE_RATE, json.dumps(sorted(set(WAKE_WORDS)) + ["[unk]"]))


def wait_for_wake(mic, vosk_model, vs):
    """Standby. Returns "wake", "announce", "muted" or "stop". Only "Hey Vision" said near the
    mirror counts (WAKE_NEAR): people talking across the room do not wake it."""
    rec = _new_wake_recognizer(vosk_model)
    recent = deque(maxlen=15)                     # loudness of the last 1.5 s
    checked = 0.0
    while not _stop.is_set():
        if not _announce.empty():
            return "announce"
        if time.time() - checked >= 1.0:
            checked = time.time()
            if vs.muted():
                return "muted"
            up = vs.online()
            if up is False and _status.get("online") is not False:
                threading.Thread(target=_warm_local_model, daemon=True).start()
            _status["online"] = up
        x = mic.read()
        mic.track(x)
        recent.append(_rms(x))
        pcm = (np.clip(x, -1.0, 1.0) * 32767).astype("<i2").tobytes()
        if rec.AcceptWaveform(pcm):
            heard = json.loads(rec.Result()).get("text", "")
        else:
            heard = json.loads(rec.PartialResult()).get("partial", "")
        if _said(heard, WAKE_WORDS):
            loud = float(np.percentile(list(recent), 80))
            if loud >= mic.threshold() * WAKE_NEAR:
                mic.voice = loud                    # this person's voice is the one to follow
                return "wake"
            rec = _new_wake_recognizer(vosk_model)  # said far away: ignore it
    return "stop"


def _log_timing(stt, answer, latency, route, engine):
    """Numbers only, no words (NFR-P1 evidence, NFR-SEC3)."""
    try:
        new = not os.path.exists(TIMINGS_FILE)
        os.makedirs(os.path.dirname(TIMINGS_FILE), exist_ok=True)
        with open(TIMINGS_FILE, "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["time", "speech_to_text_s", "answer_s", "end_of_question_to_voice_s", "route", "voice"])
            w.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), "%.2f" % stt, "%.2f" % answer, "%.2f" % latency,
                        route, engine])
    except OSError:
        pass


def _answer_and_speak(mic, speaker, vs, text, vosk_model, t_end, t_stt):
    """Writes the answer and speaks it at the same time: the first sentence is spoken while the rest
    is still being written. Returns (reply, seconds to the full answer, preroll, talked)."""
    stream, result, shown = SentenceStream(), {}, []

    def think():
        try:
            result["reply"] = ai_router.ask_ai(text, on_text=stream.feed) or ai_router.MSG_UNABLE
        except Exception as e:
            log.error("Answer failed: %s", type(e).__name__)
            result["reply"] = ai_router.MSG_UNABLE
        finally:
            result["seconds"] = time.time() - t_end - t_stt
            stream.finish(result.get("reply", ""))

    def show():                                   # the mirror shows the full reply once it is written
        if not shown and "seconds" in result:
            shown.append(True)
            route = getattr(ai_router, "LAST_ROUTE", "device")
            _set_stage("speaking", "")
            vs.speaking(result["reply"], mode=route if route in ("cloud", "local", "limited") else None,
                        heard=text)
            emit("assistant_message", text=result["reply"])

    thinker = threading.Thread(target=think, daemon=True)
    thinker.start()
    preroll, talked = speak_and_listen(mic, speaker, stream, vosk_model, on_tick=show)
    thinker.join(10.0 if preroll is not None else None)
    if preroll is None:
        show()
    return result.get("reply", ai_router.MSG_UNABLE), result.get("seconds", 0.0), preroll, talked


def _session(mic, whisper, vs, speaker, vosk_model=None):
    """One conversation after the wake word. Returns why it ended."""
    misses, wait, first = 0, FIRST_WAIT, True
    ignore, preroll, barged, carry = 0.35, None, False, ""
    while not _stop.is_set():
        if vs.muted():
            return "muted"
        _set_stage("listening", "Listening.")
        vs.listening(keep=not first)
        if mic.stream is None:
            mic.open()
        audio = record(mic, wait, ignore=0.0 if preroll else ignore, preroll=preroll)
        first, preroll, ignore = False, None, 0.0
        floor = _focus_floor(mic)
        mic.close()                                   # the mic rests while the words are worked out
        if audio is None:
            return "quiet"                            # nobody spoke: back to standby
        t_end = time.time()
        _set_stage("transcribing", "Converting speech to text.")
        vs.transcribing()
        text, stt_route = speech_to_text(whisper, clean_audio(audio, floor), vs)
        del audio                                     # audio only ever lives in memory (NFR-SEC3)
        t_stt = time.time() - t_end
        if carry:                                     # they were still talking before Vision answered
            if text and (is_end_chat(text) or is_exit_command(text)):
                return "bye"
            text, carry = (carry + " " + text).strip(), ""
        if not text:
            if barged:
                return "quiet"                        # only a noise over Vision's voice: stay quiet
            misses += 1
            vs.not_understood(NOT_CAUGHT)
            emit("assistant_message", text=NOT_CAUGHT)
            speaker.speak(NOT_CAUGHT)                 # UC-2 alternate 2, then listen again
            if misses >= 2:
                return "not_heard"
            wait = FOLLOWUP_WAIT
            continue
        misses = 0
        emit("user_message", text=text)
        if is_end_chat(text) or is_exit_command(text):
            if not barged:                            # "stop" said over Vision: it just goes quiet
                vs.speaking(GOODBYE, heard=text)
                emit("assistant_message", text=GOODBYE)
                speaker.speak(GOODBYE)
            return "bye"
        barged = False
        _set_stage("thinking", "Answering.")
        vs.thinking(heard=text)
        reply, t_answer, preroll, talked = _answer_and_speak(mic, speaker, vs, text, vosk_model, t_end, t_stt)
        barged = preroll is not None
        if barged and not talked:
            carry = text                              # Vision had not spoken yet: keep their first words
        route = getattr(ai_router, "LAST_ROUTE", "device")
        started = getattr(speaker, "audio_started", 0.0)
        latency = (started if started >= t_end else time.time()) - (t_end - END_SILENCE)
        _log_timing(t_stt, t_answer, latency, route, "%s/%s-stt" % (getattr(speaker, "engine", ""), stt_route))
        if reply in (ai_router.MSG_UNABLE, ai_router.MSG_LIMITED) and not barged:
            return "failed"                           # UC-2 alternate 3: back to passive mode
        wait = FOLLOWUP_WAIT
        ignore = 0.3                                  # skip the room's echo of Vision's last words
    return "stop"


def _say_announcements(vs, speaker):
    while not _announce.empty():
        text = _announce.get_nowait()
        vs.speaking(text)
        emit("assistant_message", text=text)
        speaker.chime()
        speaker.speak(text)


class _NoHub(object):
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def _main():
    global _speaker
    vs = mirror_link.VisionSync(role="voice", timeout=0.5) if mirror_link.VisionSync else _NoHub()
    speaker = _speaker = _speaker or Speaker(online=mirror_link.online)
    mic, models = None, None
    vs.idle()
    while not _stop.is_set():
        try:
            if models is None:
                _set_stage("loading", "Loading voice models.")
                models = load_models()
                threading.Thread(target=_warm_local_model, daemon=True).start()
            if mic is None:
                mic = Mic()
            if mic.stream is None:
                mic.open()
            _status["error"] = ""
            _set_stage("standby", 'Say "Hey Vision".')
            why = wait_for_wake(mic, models[0], vs)
            if why == "stop":
                break
            if why == "muted":
                mic.close()
                vs.idle()
                _set_stage("muted", "Muted in the phone app.")
                while vs.muted() and not _stop.wait(1.0):
                    while not _announce.empty():
                        vs.speaking(_announce.get_nowait())     # show reminders, stay silent
                continue
            if why == "announce":
                mic.close()
                _say_announcements(vs, speaker)
                continue
            emit("wake_detected")
            threading.Thread(target=speaker.chime, daemon=True).start()     # SRS UC-2: audible chime
            mirror_link.warm_openai()                 # connect to the cloud while the user speaks
            _opus_args()                              # and check once that ffmpeg can make Opus
            _session(mic, models[1], vs, speaker, models[0])
            vs.idle()
        except Exception as error:
            message = str(error) or type(error).__name__
            _status["error"] = message
            log.error("Voice assistant problem: %s", message)
            emit("error", message=message)
            _set_stage("error", message)
            if models is None:
                vs.close()            # the mirror then says "Voice assistant is not running"
            _stop.wait(30 if models is None else 5)
        finally:
            if mic is not None:
                mic.close()
            with ai_router._router_lock:          # the session is over: forget it (NFR-SEC3)
                ai_router.reset_conversation_history()
            mirror_skills.reset()
    vs.idle()
    vs.close()
    _status["enabled"] = False
    emit("voice_enabled", enabled=False)
    _set_stage("disabled", "Voice assistant is off.")


# ------------------------------------------------------------------ API used by api_server.py
def announce(text):
    """Speaks a reminder between conversations; at once when the voice loop is not running."""
    text = str(text or "").strip()
    if not text:
        return
    if _thread is not None and _thread.is_alive() and _status["stage"] not in ("loading", "error", "off", "disabled"):
        _announce.put(text)
    else:
        threading.Thread(target=_say_now, args=(text,), daemon=True).start()


def _say_now(text):
    global _speaker
    try:
        mirror_link.show_reply(text)
        _speaker = _speaker or Speaker(online=mirror_link.online)
        _speaker.chime()
        _speaker.speak(text)
    except Exception as e:
        log.warning("Could not speak a reminder: %s", type(e).__name__)


def get_voice_status():
    alive = bool(_thread and _thread.is_alive())
    return {"enabled": bool(_status["enabled"] and alive), "running": alive, "stopping": _stop.is_set(),
            "stage": _status["stage"], "error": _status["error"]}


def start_voice_service(callback=None):
    global _thread, _callback
    with _lock:
        if _thread is not None and _thread.is_alive():
            return {"started": False, "message": "Voice service is already running."}
        _callback = callback
        _stop.clear()
        _status["enabled"] = True
        _thread = threading.Thread(target=_main, name="VisionVoice", daemon=True)
        _thread.start()
    emit("voice_enabled", enabled=True)
    return {"started": True, "message": "Voice service is starting."}


def stop_voice_service():
    _stop.set()
    _status["enabled"] = False
    emit("voice_enabled", enabled=False)
    emit("status", status="stopping", message="Stopping voice assistant.")
    return {"stopped": True, "message": "Voice service stop requested."}


def main():
    import signal
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    def on_event(event):
        if event.get("type") == "status":
            log.info("%s %s", event.get("status"), event.get("message", ""))

    start_voice_service(on_event)
    signal.signal(signal.SIGTERM, lambda *_: stop_voice_service())
    try:
        while _thread is not None and _thread.is_alive():
            _thread.join(0.5)
    except KeyboardInterrupt:
        stop_voice_service()


if __name__ == "__main__":
    main()
