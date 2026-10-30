"""Opt-in native Hermes checks with fake credentials and no outbound networking."""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
BASE = "https://aixy-contract.invalid/v1"
OTHER_BASE = "https://other-contract.invalid/v1"
MODEL = "aixy/synthetic-contract"
FAKE_KEY = "gak_fake_native_contract_not_a_real_key"
HEADER = "X-Aixy-Session-ID"
MESSAGES = [{"role": "user", "content": "Return synthetic data."}]
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "synthetic_echo",
            "description": "Echo synthetic data",
            "parameters": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
        },
    }
]


@unittest.skipUnless(os.environ.get("AIXY_HERMES_SOURCE"), "Set AIXY_HERMES_SOURCE to opt in")
class NativeHermesContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = Path(os.environ["AIXY_HERMES_SOURCE"]).resolve()
        if not (source / "hermes_cli" / "config.py").is_file():
            raise ValueError("AIXY_HERMES_SOURCE must point to a prepared Hermes source checkout")
        cls.stack = contextlib.ExitStack()
        cls.addClassCleanup(cls.stack.close)
        cls.profile = Path(
            cls.stack.enter_context(tempfile.TemporaryDirectory(prefix="aixy-native-"))
        )
        # Keep HOME unchanged; all Hermes state is selected through HERMES_HOME.
        environment = {name: os.environ[name] for name in ("HOME", "PATH") if name in os.environ}
        environment.update(
            HERMES_HOME=str(cls.profile),
            HERMES_RUNTIME_DIR=str(cls.profile / "runtime"),
            HERMES_NONINTERACTIVE="1",
            HERMES_DISABLE_LAZY_INSTALLS="1",
            HERMES_ENABLE_PROJECT_PLUGINS="0",
            PYTHONDONTWRITEBYTECODE="1",
        )
        cls.stack.enter_context(patch.dict(os.environ, environment, clear=True))
        cls.stack.enter_context(patch.object(sys, "path", [str(source), *sys.path]))
        cls.stack.enter_context(patch.object(sys, "dont_write_bytecode", True))
        cls.outbound_attempts = []

        def deny_network(*_args, **_kwargs):
            cls.outbound_attempts.append("outbound socket attempted")
            raise AssertionError("Native contract tests prohibit outbound networking")

        for target, name in (
            (socket, "create_connection"),
            (socket, "getaddrinfo"),
            (socket.socket, "connect"),
            (socket.socket, "connect_ex"),
        ):
            cls.stack.enter_context(patch.object(target, name, side_effect=deny_network))
        # Do not recover/update a checkout or load project dotenv during a test import.
        for name in ("restore_interrupted_pull", "recover_if_needed"):
            cls.stack.enter_context(patch(f"hermes_cli._early_recovery.{name}", return_value=False))
        cls.stack.enter_context(patch("hermes_cli.env_loader.load_hermes_dotenv", return_value=[]))
        spec = importlib.util.spec_from_file_location(
            "aixy_native_setup", PLUGIN_ROOT / "aixy_setup.py"
        )
        cls.setup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.setup)
        cls.config = importlib.import_module("hermes_cli.config")
        if not Path(cls.config.__file__).resolve().is_relative_to(source):
            raise ValueError("Hermes was already imported from a different source checkout")
        cls.runtime = importlib.import_module("hermes_cli.runtime_provider")
        cls.aux = importlib.import_module("agent.auxiliary_client")
        cls.tags = importlib.import_module("agent.portal_tags")
        cls.helpers = importlib.import_module("agent.chat_completion_helpers")
        cls.metadata = importlib.import_module("agent.model_metadata")
        cls.scope = importlib.import_module("agent.prompt_cache_scope")
        cls.Agent = importlib.import_module("run_agent").AIAgent
        cls.SessionDB = importlib.import_module("hermes_state").SessionDB
        cls.httpx = importlib.import_module("httpx")
        cls.OpenAI = importlib.import_module("openai").OpenAI
        # Supply endpoint metadata instead of probing a real model service.
        cls.stack.enter_context(
            patch.object(
                cls.metadata,
                "fetch_endpoint_model_metadata",
                return_value={MODEL: {"id": MODEL, "context_length": 64000}},
            )
        )
        cls.stack.enter_context(patch.object(cls.metadata, "fetch_model_metadata", return_value={}))
        cls.stack.enter_context(patch("agent.models_dev.fetch_models_dev", return_value={}))
        cls.stack.enter_context(
            patch.object(cls.metadata, "_query_ollama_api_show", return_value=None)
        )

    @classmethod
    def tearDownClass(cls):
        if cls.outbound_attempts:
            raise AssertionError(cls.outbound_attempts)

    def setUp(self):
        config = self.setup._inference_config(
            {
                "agent": {"environment_probe": False},
                "providers": {"other": {"api": OTHER_BASE, "api_key": "fake-other-key"}},
            },
            BASE,
            MODEL,
        )
        self.config.save_config(config, merge_existing=False)
        self.assertTrue(self.config.save_env_value_secure("AIXY_API_KEY", FAKE_KEY)["success"])
        self.addCleanup(self.aux.reset_runtime_main, self.aux.set_runtime_main("", ""))
        self.addCleanup(
            self.tags.reset_conversation_context, self.tags.set_conversation_context(None)
        )
        self.addCleanup(self.tags.reset_affinity_scope, self.tags.set_affinity_scope(None))

    def agent(self, session="synthetic-A", api_mode="chat_completions", base_url=BASE):
        agent = self.Agent(
            api_key=FAKE_KEY,
            base_url=base_url,
            provider="aixy" if base_url == BASE else "other",
            model=MODEL,
            api_mode=api_mode,
            session_id=session,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            enabled_toolsets=[],
        )
        self.addCleanup(agent.close)
        return agent

    def test_saved_configuration_resolves_to_aixy_without_pinning_context(self):
        runtime = self.runtime.resolve_runtime_provider(requested="aixy", target_model=MODEL)
        self.assertEqual(runtime["base_url"], BASE)
        self.assertEqual(runtime["api_key"], FAKE_KEY)
        self.assertEqual(runtime["api_mode"], "chat_completions")
        raw = self.config.require_readable_config_before_write()
        self.assertEqual(raw["providers"]["aixy"]["api_key"], "${AIXY_API_KEY}")
        self.assertNotIn("context_length", raw["model"])
        self.assertEqual(self.config.get_custom_provider_session_affinity_header(BASE), HEADER)
        self.assertEqual(self.config.get_custom_provider_session_affinity_header(OTHER_BASE), "")

    def test_main_builders_preserve_tools_and_dynamic_session_scope(self):
        agent = self.agent()
        first = self.helpers.build_api_kwargs(agent, MESSAGES, TOOLS)
        self.assertEqual(first["tools"], TOOLS)
        self.assertEqual(first["model"], MODEL)
        self.assertEqual(first["extra_headers"][HEADER], "synthetic-A")
        self.assertEqual(
            self.helpers.build_api_kwargs(agent, MESSAGES)["extra_headers"], first["extra_headers"]
        )
        second = self.helpers.build_api_kwargs(self.agent("synthetic-B"), MESSAGES)
        self.assertEqual(second["extra_headers"][HEADER], "synthetic-B")
        other = self.helpers.build_api_kwargs(self.agent(base_url=OTHER_BASE), MESSAGES)
        self.assertNotIn(HEADER, other.get("extra_headers", {}))
        responses = self.agent(api_mode="codex_responses")
        kwargs = self.helpers.build_api_kwargs(responses, MESSAGES, TOOLS)
        kwargs = responses._get_transport().preflight_kwargs(
            kwargs, allow_stream=False, is_github_responses=False, sanitize_harmony_tokens=False
        )
        self.assertEqual(kwargs["extra_headers"][HEADER], "synthetic-A")
        self.assertNotIn("X-Request-ID", first["extra_headers"])

    def test_auxiliary_route_and_header_follow_the_selected_provider(self):
        self.aux.set_runtime_main(
            "aixy",
            MODEL,
            base_url=BASE,
            api_key=FAKE_KEY,
            api_mode="chat_completions",
            session_id="synthetic-A",
        )
        client, model = self.aux.resolve_provider_client("aixy", model=MODEL)
        self.addCleanup(client.close)
        self.assertEqual(str(client.base_url).rstrip("/"), BASE)
        self.assertEqual(client.api_key, FAKE_KEY)
        self.assertEqual(model, MODEL)
        kwargs = self.aux._build_call_kwargs("aixy", MODEL, MESSAGES, base_url=BASE)
        self.assertEqual(kwargs["extra_headers"][HEADER], "synthetic-A")
        other = self.aux._build_call_kwargs("other", MODEL, MESSAGES, base_url=OTHER_BASE)
        self.assertNotIn(HEADER, other.get("extra_headers", {}))

    def test_compaction_keeps_scope_and_new_conversation_resets_it(self):
        db = self.SessionDB(self.profile / f"{self.id().split('.')[-1]}.sqlite")
        self.addCleanup(db.close)
        db.create_session("before", "cli", session_key="synthetic-chat")
        holder = SimpleNamespace(
            session_id="before",
            platform="cli",
            _session_db=db,
            _gateway_session_key="synthetic-chat",
        )
        before = self.scope.declared_conversation_scope(holder)
        self.assertTrue(before)
        db.end_session("before", "compression")
        db.create_session(
            "compressed", "cli", session_key="synthetic-chat", parent_session_id="before"
        )
        holder.session_id = "compressed"
        self.assertEqual(self.scope.declared_conversation_scope(holder), before)
        db.end_session("compressed", "session_reset")
        db.create_session("new", "cli", session_key="synthetic-chat")
        holder.session_id = "new"
        self.assertNotEqual(self.scope.declared_conversation_scope(holder), before)
        self.tags.set_conversation_context(before)
        kwargs = self.helpers.build_api_kwargs(self.agent("physical-id"), MESSAGES)
        self.assertEqual(kwargs["extra_headers"][HEADER], before)

    def test_context_metadata_fallback_and_explicit_override(self):
        self.assertEqual(
            self.metadata._resolve_custom_endpoint_context_length(MODEL, BASE, FAKE_KEY, "aixy"),
            64000,
        )
        with patch.object(self.metadata, "fetch_endpoint_model_metadata", return_value={}):
            self.assertEqual(
                self.metadata._resolve_custom_endpoint_context_length(
                    MODEL, BASE, FAKE_KEY, "aixy"
                ),
                256000,
            )
        self.assertEqual(
            self.metadata.get_model_context_length(MODEL, config_context_length=96000), 96000
        )

    @staticmethod
    def completion():
        return {
            "id": "synthetic-completion",
            "object": "chat.completion",
            "created": 1,
            "model": MODEL,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "synthetic-call",
                                "type": "function",
                                "function": {
                                    "name": "synthetic_echo",
                                    "arguments": '{"value":"synthetic"}',
                                },
                            }
                        ],
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 1,
                "total_tokens": 11,
                "prompt_tokens_details": {"cached_tokens": 8},
            },
        }

    def test_sdk_tool_call_and_native_normalizer_keep_cache_usage(self):
        agent = self.agent()
        kwargs = self.helpers.build_api_kwargs(agent, MESSAGES, TOOLS)
        captured = []

        def respond(request):
            captured.append(request)
            self.assertEqual(request.url.path, "/v1/chat/completions")
            self.assertEqual(request.headers[HEADER], "synthetic-A")
            self.assertEqual(request.headers["Authorization"], "Bearer " + FAKE_KEY)
            self.assertEqual(json.loads(request.content)["tools"], TOOLS)
            return self.httpx.Response(200, json=self.completion())

        with self.OpenAI(
            api_key=FAKE_KEY,
            base_url=BASE,
            max_retries=0,
            http_client=self.httpx.Client(transport=self.httpx.MockTransport(respond)),
        ) as client:
            response = client.chat.completions.create(**kwargs)
        normalized = agent._get_transport().normalize_response(response)
        self.assertEqual(normalized.tool_calls[0].name, "synthetic_echo")
        self.assertEqual(normalized.tool_calls[0].id, "synthetic-call")
        self.assertEqual(json.loads(normalized.tool_calls[0].arguments), {"value": "synthetic"})
        self.assertEqual(normalized.finish_reason, "tool_calls")
        self.assertEqual(
            agent._get_transport().extract_cache_stats(response),
            {"cached_tokens": 8, "creation_tokens": 0},
        )
        self.assertEqual(len(captured), 1)

    def test_sdk_stream_decodes_text_and_usage_through_native_request(self):
        agent = self.agent()
        kwargs = self.helpers.build_api_kwargs(agent, MESSAGES, TOOLS)

        def respond(request):
            body = json.loads(request.content)
            self.assertTrue(body["stream"])
            self.assertEqual(request.headers[HEADER], "synthetic-A")
            chunks = [
                {
                    "choices": [
                        {"index": 0, "delta": {"content": "synthetic"}, "finish_reason": None}
                    ]
                },
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                {"choices": [], "usage": self.completion()["usage"]},
            ]
            events = [
                json.dumps(
                    {
                        "id": "synthetic-stream",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": MODEL,
                        **chunk,
                    }
                )
                for chunk in chunks
            ]
            return self.httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content="".join(f"data: {event}\n\n" for event in events) + "data: [DONE]\n\n",
            )

        with self.OpenAI(
            api_key=FAKE_KEY,
            base_url=BASE,
            max_retries=0,
            http_client=self.httpx.Client(transport=self.httpx.MockTransport(respond)),
        ) as client:
            chunks = list(
                client.chat.completions.create(
                    **kwargs, stream=True, stream_options={"include_usage": True}
                )
            )
        self.assertEqual(
            "".join(chunk.choices[0].delta.content or "" for chunk in chunks if chunk.choices),
            "synthetic",
        )
        self.assertEqual(
            agent._get_transport().extract_cache_stats(chunks[-1]),
            {"cached_tokens": 8, "creation_tokens": 0},
        )


if __name__ == "__main__":
    unittest.main()
