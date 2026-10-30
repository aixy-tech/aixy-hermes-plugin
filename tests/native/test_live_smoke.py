"""Fake-only checks for the manually armed live runner; no real gateways or keys."""

from __future__ import annotations

import json
import os
import unittest
from decimal import Decimal
from types import SimpleNamespace

from live_smoke import HEADER, PHASES, RequestGate, Settings, SmokeFailure, run_smoke


def settings():
    return Settings(
        "https://smoke.invalid/v1",
        "synthetic-model",
        64000,
        Decimal("1"),
        Decimal("1"),
        output_limit=64,
    )


def request(**overrides):
    body = {
        "model": "synthetic-model",
        "messages": [{"role": "user", "content": "synthetic"}],
        "max_tokens": 64,
        "reasoning_effort": "low",
        **overrides,
    }
    return SimpleNamespace(
        url="https://smoke.invalid/v1/chat/completions",
        method="POST",
        headers={HEADER: "synthetic-A"},
        content=json.dumps(body).encode(),
    )


class RequestGateTests(unittest.TestCase):
    def test_fake_responder_never_receives_raw_network_permission(self):
        gate = RequestGate(settings(), "synthetic-run")

        def fake(_request):
            self.assertFalse(gate.local.allowed)
            return SimpleNamespace(status_code=200)

        gate.responder = fake
        gate.arm("tool", "synthetic-A")
        gate.send(request(), lambda: self.fail("Fake responder used the real transport"))

    def test_one_send_per_phase_including_a_failed_transport(self):
        gate = RequestGate(settings(), "synthetic-run")
        gate.arm("tool", "synthetic-A")

        def fail():
            raise OSError("synthetic transport failure")

        with self.assertRaises(OSError):
            gate.send(request(), fail)
        self.assertEqual(len(gate.rows), 1)
        with self.assertRaises(SmokeFailure):
            gate.send(request(), lambda: self.fail("A hidden retry reached transport"))
        self.assertEqual(gate.blocked, 1)

    def test_wrong_url_model_cap_size_and_session_never_reach_transport(self):
        mutations = [
            lambda value: setattr(value, "url", "https://foreign.invalid/v1/chat/completions"),
            lambda value: setattr(value, "url", "https://smoke.invalid/v1/models"),
            lambda value: setattr(
                value, "content", json.dumps({"model": "wrong", "max_tokens": 64}).encode()
            ),
            lambda value: setattr(
                value,
                "content",
                json.dumps({"model": "synthetic-model", "max_tokens": 256}).encode(),
            ),
            lambda value: setattr(
                value,
                "content",
                json.dumps(
                    {"model": "synthetic-model", "max_tokens": 256, "max_completion_tokens": 64}
                ).encode(),
            ),
            lambda value: setattr(value, "content", b" " * 8193),
            lambda value: value.headers.update({HEADER: "foreign-session"}),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                gate = RequestGate(settings(), "synthetic-run")
                gate.arm("tool", "synthetic-A")
                value = request()
                mutate(value)
                with self.assertRaises((SmokeFailure, ValueError)):
                    gate.send(value, lambda: self.fail("Rejected input reached transport"))
                self.assertEqual(len(gate.rows), 0)

    def test_unknown_usage_stops_without_a_fabricated_zero_cost(self):
        gate = RequestGate(settings(), "synthetic-run")
        gate.arm("tool", "synthetic-A")
        gate.send(request(), lambda: SimpleNamespace(status_code=200))
        with self.assertRaises(SmokeFailure):
            gate.usage(SimpleNamespace(usage=None))
        self.assertNotIn("input_tokens", gate.rows[0])

    def test_no_live_execution_without_explicit_authorization(self):
        with self.assertRaisesRegex(SmokeFailure, "execution_not_authorized"):
            run_smoke(settings(), "/unused", "fake-key")


@unittest.skipUnless(os.environ.get("AIXY_HERMES_SOURCE"), "Set AIXY_HERMES_SOURCE to opt in")
class NativeLiveRunnerTests(unittest.TestCase):
    def fake_run(self, cache_read=2048, tool_status=200):
        import httpx

        seen = []

        def respond(req):
            body = json.loads(req.content)
            self.assertEqual(body["reasoning_effort"], "low")
            phase = PHASES[len(seen)]
            seen.append(phase)
            if phase == "tool" and tool_status != 200:
                return httpx.Response(tool_status)
            message = {"role": "assistant", "content": "SYNTHETIC_OK"}
            finish = "stop"
            if phase == "tool":
                message = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "synthetic-tool",
                            "type": "function",
                            "function": {"name": "synthetic_add", "arguments": '{"a":2,"b":3}'},
                        }
                    ],
                }
                finish = "tool_calls"
            elif phase == "tool_result":
                self.assertTrue(
                    any(
                        item.get("role") == "tool" and item.get("content") == "5"
                        for item in body["messages"]
                    )
                )
                message["content"] = "5"
            usage = {
                "prompt_tokens": 3000 if phase.startswith("cache_") else 100,
                "completion_tokens": 8,
                "total_tokens": 3008 if phase.startswith("cache_") else 108,
                "prompt_tokens_details": {
                    "cached_tokens": cache_read if phase == "cache_reuse" else 0
                },
            }
            if phase == "stream":
                self.assertTrue(body["stream"])
                chunks = [
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant", "content": "SYNTHETIC_OK"},
                                "finish_reason": None,
                            }
                        ]
                    },
                    {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                    {"choices": [], "usage": usage},
                ]
                events = [
                    json.dumps(
                        {
                            "id": "synthetic-stream",
                            "object": "chat.completion.chunk",
                            "created": 1,
                            "model": "synthetic-model",
                            **chunk,
                        }
                    )
                    for chunk in chunks
                ]
                return httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    stream=httpx.ByteStream(
                        (
                            "".join(f"data: {event}\n\n" for event in events) + "data: [DONE]\n\n"
                        ).encode()
                    ),
                )
            return httpx.Response(
                200,
                json={
                    "id": "synthetic-response",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "synthetic-model",
                    "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                    "usage": usage,
                },
            )

        result = run_smoke(
            settings(), os.environ["AIXY_HERMES_SOURCE"], "fake-native-smoke-key", responder=respond
        )
        return result, seen

    def test_seven_phases_use_real_hermes_main_stream_aux_and_no_network(self):
        result, seen = self.fake_run()
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(seen, list(PHASES))
        self.assertEqual(result["sent_requests"], 7)
        self.assertEqual(result["blocked_requests"], 0)
        self.assertNotEqual(
            result["requests"][0]["session_id"], result["requests"][4]["session_id"]
        )
        self.assertTrue(result["requests"][2]["sse_done"])
        self.assertEqual(result["requests"][-1]["cached_tokens"], 2048)
        self.assertNotIn("fake-native-smoke-key", json.dumps(result))

    def test_cache_miss_is_inconclusive_without_an_extra_request(self):
        result, seen = self.fake_run(cache_read=0)
        self.assertEqual(result["status"], "inconclusive", result)
        self.assertEqual(result["failure"], "cache_hit_not_observed")
        self.assertEqual(seen, list(PHASES))
        self.assertEqual(result["sent_requests"], 7)

    def test_http_error_stops_after_one_counted_send(self):
        result, seen = self.fake_run(tool_status=503)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(seen, ["tool"])
        self.assertEqual(result["sent_requests"], 1)
        self.assertFalse(result["billing_complete"])
        self.assertIsNone(result["upper_cost_usd"])


if __name__ == "__main__":
    unittest.main()
