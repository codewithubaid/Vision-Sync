"""
test_voice_fast.py - offline checks for the faster voice loop: online speech-to-text, talking over
Vision to stop it (barge-in) and the voice that can be cut short. No microphone, speaker or internet.
    ~/.venvs/visionsync-voice/bin/python -m unittest -v test_voice_fast
"""
import os
import shutil
import sys
import threading
import time
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mirror_link            # noqa: E402
import voice_service as vsvc  # noqa: E402
import tts                    # noqa: E402


class FakeHub(object):
    def __init__(self, online=True):
        self._online = online

    def online(self):
        return self._online

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class FakeMic(object):
    """Gives one 0.1 s block per read, at the given loudness."""

    def __init__(self, levels):
        self.levels, self.stream, self.voice = list(levels), object(), 0.0

    def read(self):
        time.sleep(0.1)
        level = self.levels.pop(0) if self.levels else 0.0
        return np.full(1600, level, dtype=np.float32)

    def threshold(self):
        return 0.01

    def open(self):
        self.stream = object()

    def close(self):
        self.stream = None


class FakeSpeaker(object):
    def __init__(self, seconds):
        self.seconds, self.stopped, self.playing = seconds, threading.Event(), False
        self.audio_started, self.engine, self.spoken, self.times = 0.0, "fake", [], []

    def prepare(self):
        self.stopped.clear()

    def speak_stream(self, parts, fresh=True):
        self.audio_started = 0.0
        for part in parts:
            if self.stopped.is_set():
                continue                            # drained after an interruption
            self.spoken.append(part)
            self.times.append(time.time())
            self.audio_started = self.audio_started or time.time()
            self.playing = True
            self.stopped.wait(self.seconds)
            self.playing = False
        return True

    def speak(self, text, fresh=True):
        return self.speak_stream([text], fresh)

    def stop(self):
        self.stopped.set()


class SpeechToTextTests(unittest.TestCase):
    def setUp(self):
        vsvc._stt_off_until[0] = 0.0
        self.http = mock.MagicMock()
        for p in (mock.patch.object(mirror_link, "http", return_value=self.http),
                  mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}),
                  mock.patch.object(vsvc, "STT_PROVIDER", "auto"),
                  mock.patch.object(vsvc, "STT_MODEL", "gpt-transcribe"),
                  mock.patch.object(vsvc.log, "warning")):          # keep the install output clean
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(lambda: vsvc._stt_off_until.__setitem__(0, 0.0))
        self.audio = np.zeros(16000, dtype=np.float32)

    def answer(self, text):
        self.http.post.return_value = mock.Mock(status_code=200, json=lambda: {"text": text})

    def test_online_uses_openai_from_memory(self):
        self.answer("What's the weather today?")
        with mock.patch.object(vsvc, "transcribe") as local:
            self.assertEqual(vsvc.speech_to_text(None, self.audio, FakeHub(True)),
                             ("What's the weather today?", "cloud"))
        local.assert_not_called()
        args, kwargs = self.http.post.call_args
        self.assertEqual(args[0], "https://api.openai.com/v1/audio/transcriptions")
        self.assertEqual(kwargs["data"]["model"], "gpt-transcribe")
        self.assertNotIn("language", kwargs["data"])            # gpt-transcribe does not take it
        name, data, mime = kwargs["files"]["file"]
        self.assertTrue((data.startswith(b"OggS") and mime == "audio/ogg")
                        or (data.startswith(b"RIFF") and mime == "audio/wav"))
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer sk-test")

    def test_offline_uses_whisper_on_the_mirror(self):
        with mock.patch.object(vsvc, "transcribe", return_value="hello"):
            self.assertEqual(vsvc.speech_to_text(None, self.audio, FakeHub(False)), ("hello", "local"))
        self.http.post.assert_not_called()

    def test_failure_falls_back_and_the_next_question_does_not_wait(self):
        self.http.post.side_effect = OSError("network down")
        with mock.patch.object(vsvc, "transcribe", return_value="hello"):
            self.assertEqual(vsvc.speech_to_text(None, self.audio, FakeHub(True)), ("hello", "local"))
            self.assertEqual(vsvc.speech_to_text(None, self.audio, FakeHub(True)), ("hello", "local"))
        self.assertEqual(self.http.post.call_count, 1)

    def test_repeated_hint_is_not_a_question(self):
        self.answer(vsvc.STT_PROMPT)
        self.assertEqual(vsvc.speech_to_text(None, self.audio, FakeHub(True)), ("", "cloud"))
        self.answer("Turn on security mode")                      # a real command that uses hint words
        self.assertEqual(vsvc.speech_to_text(None, self.audio, FakeHub(True))[0], "Turn on security mode")

    def test_small_opus_upload_and_wav_if_refused(self):
        self.http.post.side_effect = [mock.Mock(status_code=400, json=lambda: {}),
                                      mock.Mock(status_code=200, json=lambda: {"text": "Hello there"})]
        with mock.patch.object(vsvc, "_ogg_bytes", return_value=b"OggS" + b"\0" * 200), \
                mock.patch.object(vsvc, "_STT_OGG", [True]):
            self.assertEqual(vsvc.speech_to_text(None, self.audio, FakeHub(True)), ("Hello there", "cloud"))
            self.assertEqual(self.http.post.call_args_list[0].kwargs["files"]["file"][2], "audio/ogg")
            self.assertEqual(self.http.post.call_args_list[1].kwargs["files"]["file"][2], "audio/wav")
            self.assertFalse(vsvc._STT_OGG[0])

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is not installed here")
    def test_opus_is_much_smaller_than_wav(self):
        if not vsvc._opus_args(wait=True):
            self.skipTest("this ffmpeg cannot make Opus (%s): WAV is sent, which works too" % vsvc._OPUS["why"])
        audio = (0.2 * np.sin(np.arange(48000) / 5.0)).astype(np.float32)
        ogg = vsvc._ogg_bytes(audio)
        self.assertTrue(ogg and ogg.startswith(b"OggS"))
        self.assertLess(len(ogg) * 4, len(vsvc._wav_bytes(audio)))

    def test_wav_is_sent_when_ffmpeg_cannot_make_opus(self):
        with mock.patch.object(vsvc, "_opus_args", return_value=None):
            self.assertEqual(vsvc._upload(np.zeros(16000, dtype=np.float32))[2], "audio/wav")

    def test_ffmpeg_s_own_opus_encoder_when_libopus_is_missing(self):
        def run(args, **kw):
            ok = "opus" in args and "libopus" not in args
            return mock.Mock(returncode=0 if ok else 1, stdout=b"OggS" if ok else b"",
                             stderr=b"" if ok else b"Unknown encoder 'libopus'")
        with mock.patch.object(vsvc.shutil, "which", return_value="/usr/bin/ffmpeg"), \
                mock.patch.object(vsvc.subprocess, "run", side_effect=run):
            args = vsvc._find_opus()
        self.assertIn("opus", args)
        self.assertNotIn("libopus", args)

    def test_local_setting_never_uses_the_cloud(self):
        with mock.patch.object(vsvc, "STT_PROVIDER", "local"), \
                mock.patch.object(vsvc, "transcribe", return_value="x"):
            self.assertEqual(vsvc.speech_to_text(None, self.audio, FakeHub(True)), ("x", "local"))
        self.http.post.assert_not_called()


class BargeInTests(unittest.TestCase):
    @staticmethod
    def feed(detector, levels, playing=True, start=0.0):
        t, hits = start, []
        for level in levels:
            hits.append(detector.feed(level, playing, t))
            t += 0.1
        return hits, t

    def test_its_own_voice_never_stops_it(self):
        hits, _ = self.feed(vsvc.BargeIn(0.01, ratio=3.0), [0.05, 0.08, 0.06, 0.09, 0.07, 0.05, 0.08] * 6)
        self.assertFalse(any(hits))

    def test_user_talking_over_it_stops_it(self):
        det = vsvc.BargeIn(0.01, ratio=3.0)
        hits, t = self.feed(det, [0.06, 0.08, 0.07] * 5)
        more, _ = self.feed(det, [0.35, 0.40, 0.38, 0.42], start=t)
        self.assertFalse(any(hits))
        self.assertTrue(any(more))

    def test_the_start_of_its_voice_is_ignored(self):
        hits, _ = self.feed(vsvc.BargeIn(0.01, ratio=3.0), [0.001, 0.001, 0.3, 0.3, 0.3])
        self.assertFalse(any(hits))

    def test_still_talking_before_its_voice_starts(self):
        hits, _ = self.feed(vsvc.BargeIn(0.01), [0.05] * 4, playing=False)
        self.assertTrue(any(hits))


class SpeakAndListenTests(unittest.TestCase):
    def test_reply_finishes_when_only_its_echo_is_heard(self):
        speaker = FakeSpeaker(1.0)
        with mock.patch.object(vsvc, "BARGE_IN", "on"):
            out, talked = vsvc.speak_and_listen(FakeMic([0.06, 0.08, 0.07] * 20), speaker, "Hello there.", None)
        self.assertIsNone(out)
        self.assertFalse(speaker.stopped.is_set())

    def test_talking_over_the_reply_stops_it_at_once(self):
        speaker = FakeSpeaker(6.0)
        started = time.time()
        with mock.patch.object(vsvc, "BARGE_IN", "on"):
            out, talked = vsvc.speak_and_listen(FakeMic([0.06, 0.08, 0.07] * 4 + [0.4] * 20), speaker,
                                                "A long answer about many things.", None)
        self.assertTrue(talked)
        self.assertTrue(speaker.stopped.is_set())
        self.assertLess(time.time() - started, 3.0)
        self.assertAlmostEqual(vsvc._rms(out[-1]), 0.4, places=3)   # the user's first words are kept

    def test_off_just_speaks(self):
        speaker = FakeSpeaker(0.1)
        with mock.patch.object(vsvc, "BARGE_IN", "off"):
            self.assertEqual(vsvc.speak_and_listen(FakeMic([0.4] * 5), speaker, "Hi.", None), (None, True))
        self.assertEqual(speaker.spoken, ["Hi."])

    def test_first_words_are_kept_for_the_next_question(self):
        pre = [np.full(1600, 0.3, dtype=np.float32) for _ in range(5)]
        with mock.patch.object(vsvc, "END_SILENCE", 0.5):
            audio = vsvc.record(FakeMic([0.3, 0.3, 0.3] + [0.0] * 8), 3, preroll=pre)
        self.assertGreaterEqual(len(audio), 1600 * 8)


class MicTests(unittest.TestCase):
    def test_falls_back_to_the_mic_chosen_in_settings(self):
        fake = mock.MagicMock()

        def query(device, kind):
            if device == "USB Audio Device":
                raise ValueError("busy")
            return {"default_samplerate": 48000}

        fake.query_devices.side_effect = query
        with mock.patch.dict(sys.modules, {"sounddevice": fake}), mock.patch.object(vsvc.log, "warning"), \
                mock.patch.object(vsvc, "MIC_DEVICE", "USB Audio Device"):
            mic = vsvc.Mic()
            mic.open()
        self.assertEqual(mic.device, "pipewire")
        self.assertEqual(fake.InputStream.call_args.kwargs["device"], "pipewire")


class SpeakerTests(unittest.TestCase):
    def make(self):
        with mock.patch.dict(os.environ, {"PIPER_MODEL": "", "ELEVENLABS_API_KEY": ""}):
            return tts.Speaker(online=lambda: False)

    def test_sentences(self):
        self.assertEqual(tts._sentences("Sure. The weather is sunny and warm today. Take water! It is 3.5 km."),
                         ["Sure. The weather is sunny and warm today.", "Take water! It is 3.5 km."])

    def test_stop_cuts_the_sound_at_once(self):
        sp = self.make()
        with mock.patch.object(sp, "_player_cmd", return_value=["sleep", "5"]):
            started = time.time()
            t = threading.Thread(target=sp.play, args=(b"\0\0" * 100, 16000))
            t.start()
            time.sleep(0.3)
            self.assertTrue(sp.playing)
            sp.stop()
            t.join(3)
        self.assertFalse(t.is_alive())
        self.assertLess(time.time() - started, 2.5)
        self.assertFalse(sp.playing)

    def test_sentence_by_sentence_and_stop(self):
        text = "First sentence is here now. Second one follows after it. Third."
        for stop_after, expected in ((None, 3), (1, 1)):
            sp, played = self.make(), []

            def fake_play(pcm, rate):
                played.append(pcm)
                if stop_after and len(played) == stop_after:
                    sp.stop()                       # the user talks during the first sentence
                return True

            with mock.patch.object(sp, "_piper_ready", return_value=True), \
                    mock.patch.object(sp, "_piper_pcm", side_effect=lambda s: (s.encode() * 60, 22050)), \
                    mock.patch.object(sp, "play", side_effect=fake_play):
                self.assertTrue(sp.speak(text))
            self.assertEqual(len(played), expected)
            self.assertTrue(played[0].startswith(b"First"))

    def test_chime_still_plays_after_a_stop(self):
        sp = self.make()
        sp.stop()
        with mock.patch.object(sp, "_player_cmd", return_value=["true"]), \
                mock.patch.object(sp, "_run", wraps=sp._run) as run:
            sp.chime()
        self.assertFalse(run.call_args.kwargs["interruptible"])


class ConnectionTests(unittest.TestCase):
    def test_wake_word_opens_the_connection_early(self):
        http = mock.MagicMock()
        with mock.patch.object(mirror_link, "http", return_value=http), \
                mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-x"}):
            mirror_link.warm_openai()
            time.sleep(0.3)
        http.get.assert_called_once()


class NoisyHallTests(unittest.TestCase):
    def test_question_ends_when_the_speaker_stops_even_with_a_crowd(self):
        mic = FakeMic([0.03] * 3 + [0.15] * 10 + [0.03] * 60)       # crowd, the speaker, crowd again
        mic.voice = 0.15
        started = time.time()
        self.assertIsNotNone(vsvc.record(mic, 3))
        self.assertLess(time.time() - started, 3.0)                  # not 15 s of crowd

    def test_the_crowd_alone_does_not_start_a_question(self):
        mic = FakeMic([0.03] * 25)
        mic.voice = 0.15
        self.assertIsNone(vsvc.record(mic, 1.5))

    def test_someone_further_away_does_not_interrupt(self):
        det = vsvc.BargeIn(0.01, near=0.15)
        far, t = BargeInTests.feed(det, [0.05] * 10, playing=False)
        close, _ = BargeInTests.feed(det, [0.3] * 4, playing=False, start=t)
        self.assertFalse(any(far))
        self.assertTrue(any(close))

    def test_clean_audio_keeps_only_the_speaker(self):
        sr = vsvc.SAMPLE_RATE
        quiet = np.random.default_rng(0).normal(0, 0.002, sr).astype(np.float32)
        voice = (0.2 * np.sin(2 * np.pi * 220 * np.arange(sr) / sr)).astype(np.float32)
        out = vsvc.clean_audio(np.concatenate([quiet, voice, quiet]), 0.02)
        self.assertTrue(int(0.95 * sr) < len(out) < int(1.7 * sr))
        self.assertAlmostEqual(float(np.max(np.abs(out))), 0.9, places=1)

    def test_words_are_fixed(self):
        self.assertEqual(vsvc._fix_transcript("Hey Vision, what's the time?"), "what's the time?")
        self.assertEqual(vsvc._fix_transcript("Hey Vision"), "Hey Vision")
        self.assertEqual(vsvc._fix_transcript("turn on home along mode"), "turn on home alone mode")
        self.assertEqual(vsvc._fix_transcript("why fi status"), "Wi-Fi status")
        self.assertEqual(vsvc._fix_transcript("Visionary ideas"), "Visionary ideas")

    def test_wake_phrase_is_whole_words(self):
        self.assertTrue(vsvc._said("hey vision", ["hey vision"]))
        self.assertFalse(vsvc._said("division", ["vision"]))
        self.assertEqual(vsvc.WAKE_WORDS, ["hey vision"])


class SentenceStreamTests(unittest.TestCase):
    def test_sentences_are_ready_before_the_answer_is_finished(self):
        s = vsvc.SentenceStream()
        for delta in ["It is sunny", " and warm today.", " Have a", " great day."]:
            s.feed(delta)
        self.assertEqual(s.q.get_nowait(), "It is sunny and warm today.")
        self.assertTrue(s.q.empty())
        s.finish("ignored: text was streamed")
        self.assertEqual(list(s), ["Have a great day."])

    def test_hidden_reasoning_is_never_spoken(self):
        s = vsvc.SentenceStream()
        s.feed("<think>let me think</think>Hello there friend. Bye now.")
        s.finish()
        self.assertEqual(list(s), ["Hello there friend. Bye now."])

    def test_whole_reply_when_nothing_was_streamed_and_close(self):
        s = vsvc.SentenceStream()
        s.finish("Done.")
        self.assertEqual(list(s), ["Done."])
        s = vsvc.SentenceStream()
        s.close()
        self.assertEqual(list(s), [])


class ConversationTests(unittest.TestCase):
    def run_session(self, levels, heard, ask):
        mic, speaker = FakeMic(levels), FakeSpeaker(0.3)
        with mock.patch.object(vsvc, "speech_to_text", side_effect=lambda w, a, v=None: next(heard)), \
                mock.patch.object(vsvc.ai_router, "ask_ai", side_effect=ask), \
                mock.patch.object(vsvc, "TIMINGS_FILE", "/tmp/visionsync_test_timings.csv"), \
                mock.patch.object(vsvc, "FOLLOWUP_WAIT", 0.5), mock.patch.object(vsvc, "BARGE_IN", "on"):
            why = vsvc._session(mic, None, FakeHub(True), speaker, None)
        return why, speaker

    def test_speaks_the_first_sentence_while_the_answer_is_still_written(self):
        done = []

        def ask(text, on_text=None):
            on_text("It is sunny and warm today. ")
            time.sleep(0.8)
            on_text("Have a great day.")
            done.append(time.time())
            return "It is sunny and warm today. Have a great day."

        why, speaker = self.run_session([0.0] * 4 + [0.3] * 5 + [0.0] * 60,
                                        iter([("what is the weather", "cloud")]), ask)
        self.assertEqual(why, "quiet")
        self.assertEqual(speaker.spoken, ["It is sunny and warm today.", "Have a great day."])
        self.assertLess(speaker.times[0], done[0])               # talking before the answer was finished

    def test_still_talking_before_it_answers_keeps_the_whole_question(self):
        asked = []

        def ask(text, on_text=None):
            asked.append(text)
            vsvc.ai_router._cancel.clear()
            if len(asked) == 1:
                vsvc.ai_router._cancel.wait(3)                  # writing, until the person goes on talking
                return ""
            on_text("Tomorrow will be cooler.")
            return "Tomorrow will be cooler."

        levels = ([0.0] * 4 + [0.3] * 5 + [0.0] * 8               # "what is the weather" ... pause
                  + [0.0] * 3 + [0.3] * 7 + [0.0] * 8             # "... and tomorrow"
                  + [0.0] * 40)
        why, speaker = self.run_session(levels, iter([("what is the weather", "cloud"),
                                                      ("and tomorrow", "cloud")]), ask)
        self.assertEqual(asked, ["what is the weather", "what is the weather and tomorrow"])
        self.assertEqual(speaker.spoken, ["Tomorrow will be cooler."])
        self.assertEqual(why, "quiet")


class NoPaidVoiceTests(unittest.TestCase):
    def test_only_voices_on_the_jetson(self):
        self.assertFalse(hasattr(tts.Speaker, "_eleven"))
        with open(tts.__file__) as f:
            self.assertNotIn("elevenlabs.io", f.read())


if __name__ == "__main__":
    unittest.main()
