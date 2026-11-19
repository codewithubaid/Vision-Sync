"""
test_openai_router.py - offline checks for the OpenAI online model. Nothing is sent: requests.post
is replaced by fakes, so these tests need no key and no internet, and cost nothing.
    ~/.venvs/visionsync-voice/bin/python -m unittest -v test_openai_router
"""
import json
import os
import socket
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mirror_link  # noqa: E402
import ai_router    # noqa: E402


class FakeResponse:
    def __init__(self, status=200, events=None, body=None):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._events = events or []
        self._body = body if body is not None else {}
        self.text = json.dumps(self._body)
        self.raw = None

    def iter_lines(self):
        for event in self._events:
            yield ("event: " + event.get("type", "")).encode("utf-8")
            yield ("data: " + json.dumps(event)).encode("utf-8")
            yield b""

    def json(self):
        return self._body

    def close(self):
        pass


def text_events(*pieces):
    return ([{"type": "response.created"}]
            + [{"type": "response.output_text.delta", "delta": p} for p in pieces]
            + [{"type": "response.completed"}])


def tool_call_output():
    return {"output": [
        {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "ENC"},
        {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "create_task",
         "arguments": json.dumps({"title": "buy milk"})}]}


def final_message(text):
    return {"output": [{"type": "message", "id": "msg_1", "role": "assistant",
                        "content": [{"type": "output_text", "text": text, "annotations": []}]}]}


class OpenAIBase(unittest.TestCase):
    def setUp(self):
        ai_router._cancel.clear()         # an answer talked over in another test file must not stop these
        ai_router._OPENAI_DROPPED.clear()
        self.addCleanup(ai_router._OPENAI_DROPPED.clear)
        for p in (mock.patch.object(ai_router, "OPENAI_API_KEY", "sk-test"),
                  mock.patch.object(ai_router, "OPENAI_MODEL", "gpt-6.1-terra"),
                  mock.patch.object(ai_router, "OPENAI_URL", "https://api.openai.com/v1/responses"),
                  mock.patch.object(ai_router, "OPENAI_REASONING", "none"),
                  mock.patch.object(ai_router, "OPENAI_MAX_OUTPUT_TOKENS", 800),
                  mock.patch.object(ai_router, "CLOUD_PROVIDER", "openai")):
            p.start()
            self.addCleanup(p.stop)
        self.http = mock.MagicMock()                      # the shared connection (mirror_link.http)
        p = mock.patch.object(mirror_link, "http", return_value=self.http)
        p.start()
        self.addCleanup(p.stop)


class AnswerTests(OpenAIBase):
    def test_streamed_answer_and_private_request(self):
        fake = FakeResponse(events=text_events("Hello", " there."))
        with mock.patch.object(self.http, "post", return_value=fake) as post:
            out = ai_router.ask_openai([{"role": "system", "content": "SYS"},
                                        {"role": "assistant", "content": "Hi"},
                                        {"role": "user", "content": "Q"}])
        self.assertEqual(out, "Hello there.")
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://api.openai.com/v1/responses")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer sk-test")
        body = kwargs["json"]
        self.assertEqual(body["model"], "gpt-6.1-terra")
        self.assertEqual(body["instructions"], "SYS")
        self.assertEqual(body["input"], [{"role": "assistant", "content": "Hi"},
                                         {"role": "user", "content": "Q"}])
        self.assertIs(body["store"], False)
        self.assertIs(body["stream"], True)
        self.assertEqual(body["reasoning"], {"effort": "none"})
        self.assertNotIn("tools", body)

    def test_error_event_raises(self):
        fake = FakeResponse(events=[{"type": "error", "message": "boom"}])
        with mock.patch.object(self.http, "post", return_value=fake):
            with self.assertRaises(RuntimeError):
                ai_router.ask_openai([{"role": "user", "content": "Q"}])

    def test_no_credit_is_reported_as_429(self):
        fake = FakeResponse(status=429, body={"error": {"message": "You exceeded your current quota.",
                                                        "code": "insufficient_quota"}})
        with mock.patch.object(self.http, "post", return_value=fake):
            with self.assertRaises(ai_router.requests.HTTPError) as ctx:
                ai_router.ask_openai([{"role": "user", "content": "Q"}])
        self.assertIn("429", str(ctx.exception))

    def test_unsupported_reasoning_setting_is_dropped(self):
        bad = FakeResponse(status=400, body={"error": {
            "message": "Unsupported value: 'reasoning.effort' does not support 'none' with this model.",
            "param": "reasoning.effort"}})
        good = FakeResponse(events=text_events("Fine."))
        with mock.patch.object(self.http, "post", side_effect=[bad, good]) as post:
            self.assertEqual(ai_router.ask_openai([{"role": "user", "content": "Q"}]), "Fine.")
        self.assertEqual(post.call_count, 2)
        self.assertNotIn("reasoning", post.call_args.kwargs["json"])

    def test_provider_switch(self):
        with mock.patch.object(ai_router, "ask_openai", return_value="A") as oa, \
                mock.patch.object(ai_router, "ask_gemini", return_value="B") as gm:
            self.assertEqual(ai_router.ask_cloud([{"role": "user", "content": "Q"}]), "A")
            with mock.patch.object(ai_router, "CLOUD_PROVIDER", "gemini"):
                self.assertEqual(ai_router.ask_cloud([{"role": "user", "content": "Q"}]), "B")
        oa.assert_called_once()
        gm.assert_called_once()


class ToolTests(OpenAIBase):
    MESSAGES = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "add a task buy milk"}]

    def test_tool_runs_and_turn_is_replayed(self):
        first = FakeResponse(body=tool_call_output())
        second = FakeResponse(body=final_message("Added buy milk."))
        with mock.patch.object(self.http, "post", side_effect=[first, second]) as post, \
                mock.patch.object(ai_router.vision_tools, "execute_tool",
                                  return_value={"success": True, "id": 7}) as run:
            out = ai_router._openai_tool_turn([dict(m) for m in self.MESSAGES])
        self.assertEqual(out, "Added buy milk.")
        run.assert_called_once_with("create_task", {"title": "buy milk"})
        body1 = post.call_args_list[0].kwargs["json"]
        self.assertEqual(body1["include"], ["reasoning.encrypted_content"])
        self.assertTrue(body1["tools"])
        self.assertTrue(all(t["type"] == "function" and t["strict"] is False for t in body1["tools"]))
        self.assertIn("LOCAL DATE/TIME", body1["input"][-1]["content"])
        body2 = post.call_args_list[1].kwargs["json"]
        self.assertEqual([i.get("type") for i in body2["input"]][-3:],
                         ["reasoning", "function_call", "function_call_output"])
        self.assertEqual(body2["input"][-1]["call_id"], "call_1")
        self.assertEqual(json.loads(body2["input"][-1]["output"]), {"success": True, "id": 7})
        self.assertEqual(ai_router._tools_ran[0][0], "create_task")

    def test_refused_replay_is_resent_simple(self):
        refused = FakeResponse(status=400, body={"error": {
            "message": "Item 'fc_1' of type 'function_call' was provided without its required 'reasoning' item: 'rs_1'.",
            "param": "input"}})
        with mock.patch.object(self.http, "post", side_effect=[
                FakeResponse(body=tool_call_output()), refused,
                FakeResponse(body=final_message("Done, I added it."))]) as post, \
                mock.patch.object(ai_router.vision_tools, "execute_tool", return_value={"success": True}) as run:
            out = ai_router._openai_tool_turn([dict(m) for m in self.MESSAGES])
        self.assertEqual(out, "Done, I added it.")
        run.assert_called_once()                  # the action never runs twice
        body3 = post.call_args_list[2].kwargs["json"]
        self.assertNotIn("reasoning", [i.get("type") for i in body3["input"]])
        call = next(i for i in body3["input"] if i.get("type") == "function_call")
        self.assertNotIn("id", call)
        self.assertIn("reasoning", body3)         # the fast-answer setting is still sent

    def test_tool_schema_has_no_nullable(self):
        self.assertNotIn("nullable", json.dumps(ai_router._openai_tool_declarations()))


class RouterOpenAITests(OpenAIBase):
    def setUp(self):
        super().setUp()
        ai_router.reset_conversation_history()
        ai_router._cloud_blocked_until = 0.0
        for p in (mock.patch.object(ai_router, "gemini_client", True),
                  mock.patch.object(mirror_link, "client", return_value=None),
                  mock.patch.object(mirror_link, "online", return_value=True),
                  mock.patch.object(ai_router, "_is_tool_intent", return_value=False),
                  mock.patch.dict(os.environ, {"VISION_ENABLE_GEMINI": "1", "VISION_ENABLE_CLOUD": "1"})):
            p.start()
            self.addCleanup(p.stop)

    def test_online_question_is_answered_by_openai(self):
        with mock.patch.object(ai_router, "ask_openai", return_value="From OpenAI.") as oa, \
                mock.patch.object(ai_router, "ask_gemini") as gm:
            self.assertEqual(ai_router.ask_ai("Tell me a fun fact about owls"), "From OpenAI.")
        oa.assert_called_once()
        gm.assert_not_called()
        self.assertEqual(ai_router.LAST_ROUTE, "cloud")

    def test_no_credit_goes_local_and_pauses_the_cloud(self):
        err = ai_router.requests.HTTPError("OpenAI HTTP 429: You exceeded your current quota.")
        with mock.patch.object(ai_router, "ask_openai", side_effect=err) as oa, \
                mock.patch.object(ai_router, "ask_ollama", return_value="Local."):
            self.assertEqual(ai_router.ask_ai("Tell me a joke"), "Local.")
            self.assertEqual(ai_router.ask_ai("Another joke"), "Local.")
        self.assertEqual(oa.call_count, 1)
        self.assertEqual(ai_router.LAST_ROUTE, "local")


class NetworkTests(unittest.TestCase):
    def test_ipv4_only_when_switched_on(self):
        seen = []

        def fake(host, port, family=0, *args, **kwargs):
            seen.append(family)
            return []

        with mock.patch.object(mirror_link, "_orig_getaddrinfo", side_effect=fake):
            mirror_link.force_ipv4(True)
            try:
                socket.getaddrinfo("api.openai.com", 443)
            finally:
                mirror_link.force_ipv4(False)
            socket.getaddrinfo("api.openai.com", 443)
        self.assertEqual(seen, [socket.AF_INET, 0])


class StreamingTests(OpenAIBase):
    def tearDown(self):
        ai_router._stream_cb = None
        ai_router._cancel.clear()

    def test_answer_is_passed_on_while_written(self):
        got = []
        ai_router._stream_cb = got.append
        self.http.post.return_value = FakeResponse(events=text_events("Hello", " there."))
        self.assertEqual(ai_router.ask_openai([{"role": "user", "content": "Q"}]), "Hello there.")
        self.assertEqual(got, ["Hello", " there."])

    def test_broken_stream_keeps_what_was_said(self):
        self.http.post.return_value = FakeResponse(events=[
            {"type": "response.output_text.delta", "delta": "It is sunny."}, {"type": "error", "message": "boom"}])
        self.assertEqual(ai_router.ask_openai([{"role": "user", "content": "Q"}]), "It is sunny.")

    def test_interrupted_answer_does_not_fall_back_to_the_local_model(self):
        def interrupted(*args, **kwargs):
            ai_router.cancel()
            raise RuntimeError("OpenAI returned no text.")

        with mock.patch.object(ai_router, "gemini_client", True), \
                mock.patch.object(mirror_link, "client", return_value=None), \
                mock.patch.object(mirror_link, "online", return_value=True), \
                mock.patch.object(ai_router, "_is_tool_intent", return_value=False), \
                mock.patch.dict(os.environ, {"VISION_ENABLE_GEMINI": "1", "VISION_ENABLE_CLOUD": "1"}), \
                mock.patch.object(ai_router, "ask_openai", side_effect=interrupted), \
                mock.patch.object(ai_router, "ask_ollama") as local:
            ai_router.reset_conversation_history()
            ai_router._cloud_blocked_until = 0.0
            self.assertEqual(ai_router.ask_ai("Tell me a joke", on_text=lambda text: None), "")
        local.assert_not_called()


class LocalModelTests(unittest.TestCase):
    def setUp(self):
        ai_router._cancel.clear()

    def test_qwen3_answers_without_thinking_and_streams(self):
        class Reply(object):
            ok, status_code, text, raw = True, 200, "", None

            def iter_lines(self):
                yield json.dumps({"message": {"content": "Hi there."}, "done": True}).encode()

            def close(self):
                pass

        got = []
        ai_router._stream_cb = got.append
        try:
            with mock.patch.object(ai_router.requests, "post", return_value=Reply()) as post:
                out = ai_router.ask_ollama([{"role": "system", "content": "S"},
                                            {"role": "user", "content": "Hello"}], "qwen3-local")
        finally:
            ai_router._stream_cb = None
        self.assertEqual(out, "Hi there.")
        body = post.call_args.kwargs["json"]
        self.assertIs(body["think"], False)
        self.assertTrue(body["messages"][-1]["content"].endswith("/no_think"))
        self.assertEqual(got, ["Hi there."])


if __name__ == "__main__":
    unittest.main()
