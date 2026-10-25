"""Offline behavioral checks for the Hermes integration; never use a real profile."""

from __future__ import annotations

import argparse
import contextlib
import copy
import importlib.util
import io
import json
import sys
import types
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

PLUGIN_DIR = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("aixy_setup_under_test", PLUGIN_DIR / "aixy_setup.py")
assert SPEC is not None and SPEC.loader is not None
setup = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = setup
SPEC.loader.exec_module(setup)

FAKE_KEY = "gak_fake_offline_test_credential"
DEFAULT_BASE = "https://api.aixy-gateway.com/v1"
DEFAULT_MCP = "https://api.aixy-gateway.com/mcp"


class FakeHermesConfig(types.ModuleType):
    """Only in-memory state: a test can never touch ~/.hermes or a real key."""

    def __init__(self, initial=None):
        super().__init__("hermes_cli.config")
        self.__file__ = "/offline/hermes/hermes_cli/config.py"
        self.state = copy.deepcopy(initial or {})
        self.require_readable_config_before_write = Mock(side_effect=self.read)
        self.load_config = Mock(side_effect=self.read)
        self.is_managed = Mock(return_value=False)
        self.save_config = Mock(side_effect=self.save)
        self.save_env_value_secure = Mock(return_value={"success": True})

    def read(self, *args, **kwargs):
        return copy.deepcopy(self.state)

    def save(self, config, *, merge_existing=True, **kwargs):
        def merge(target, source):
            for key, value in source.items():
                if isinstance(value, dict) and isinstance(target.get(key), dict):
                    merge(target[key], value)
                else:
                    target[key] = copy.deepcopy(value)
            return target

        self.state = (
            merge(copy.deepcopy(self.state), config) if merge_existing else copy.deepcopy(config)
        )


class PluginRegistrationTests(unittest.TestCase):
    def test_registration_exposes_real_argparse_commands_without_profile_or_network_io(self):
        spec = importlib.util.spec_from_file_location(
            "aixy_hermes_plugin_under_test",
            PLUGIN_DIR / "__init__.py",
            submodule_search_locations=[str(PLUGIN_DIR)],
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        plugin = importlib.util.module_from_spec(spec)
        config = FakeHermesConfig()
        hermes = types.ModuleType("hermes_cli")
        hermes.config = config
        context = Mock()
        with patch.dict(
            sys.modules,
            {spec.name: plugin, "hermes_cli": hermes, "hermes_cli.config": config},
        ):
            with patch("builtins.input") as prompt, patch("getpass.getpass") as secret:
                with patch("urllib.request.build_opener") as network:
                    spec.loader.exec_module(plugin)
                    plugin.register(context)
            prompt.assert_not_called()
            secret.assert_not_called()
            network.assert_not_called()
        config.require_readable_config_before_write.assert_not_called()
        config.save_config.assert_not_called()
        config.save_env_value_secure.assert_not_called()
        registration = context.register_cli_command.call_args.kwargs
        self.assertEqual(registration["name"], "aixy")
        parser = argparse.ArgumentParser()
        aixy = parser.add_subparsers().add_parser(registration["name"])
        registration["setup_fn"](aixy)
        arguments = parser.parse_args(["aixy", "mcp", "--url", DEFAULT_MCP])
        self.assertEqual(arguments.aixy_command, "mcp")
        self.assertEqual(arguments.url, DEFAULT_MCP)
        self.assertIs(arguments.func, registration["handler_fn"])


class NativeMcpInvocationTests(unittest.TestCase):
    def setUp(self):
        terminal = patch.object(setup.sys.stdin, "isatty", return_value=True)
        terminal.start()
        self.addCleanup(terminal.stop)
        unattended = patch.dict(setup.os.environ, dict.fromkeys(setup._UNATTENDED_ENV, "0"))
        unattended.start()
        self.addCleanup(unattended.stop)
        hermes = types.ModuleType("hermes_cli")
        hermes.__file__ = "/offline/hermes/hermes_cli/__init__.py"
        self.hermes_module = patch.dict(sys.modules, {"hermes_cli": hermes})
        self.hermes_module.start()
        self.addCleanup(self.hermes_module.stop)

    def test_oauth_delegation_pins_profile_and_does_not_pass_a_key_in_arguments(self):
        config = FakeHermesConfig()
        config.get_hermes_home = Mock(return_value=Path("/offline/profile"))
        completed = types.SimpleNamespace(returncode=0)
        with patch.object(setup, "_config_module", return_value=config):
            with patch.dict(setup.os.environ, {"HERMES_HOME": "/different/home"}, clear=True):
                with patch.object(setup.subprocess, "run", return_value=completed) as command:
                    setup._native_mcp_add(DEFAULT_MCP)
        arguments = command.call_args.args[0]
        self.assertEqual(
            arguments,
            [
                sys.executable,
                "-m",
                "hermes_cli.main",
                "mcp",
                "add",
                "aixy",
                "--url",
                DEFAULT_MCP,
                "--auth",
                "oauth",
            ],
        )
        self.assertEqual(command.call_args.kwargs["env"]["HERMES_HOME"], "/offline/profile")
        self.assertEqual(command.call_args.kwargs["cwd"], Path("/offline/hermes"))
        self.assertNotIn(FAKE_KEY, repr(arguments))

    def test_failed_native_oauth_is_reported_without_process_details(self):
        config = FakeHermesConfig()
        config.get_hermes_home = Mock(return_value=Path("/offline/profile"))
        completed = types.SimpleNamespace(returncode=1)
        with patch.object(setup, "_config_module", return_value=config):
            with patch.object(setup.subprocess, "run", return_value=completed):
                with self.assertRaises(setup.SetupError) as caught:
                    setup._native_mcp_add(DEFAULT_MCP)
        self.assertNotIn(FAKE_KEY, str(caught.exception))

    def test_child_does_not_inherit_yolo_or_unattended_launch_policy(self):
        config = FakeHermesConfig()
        config.get_hermes_home = Mock(return_value=Path("/offline/profile"))
        launch_flags = {"HERMES_YOLO_MODE": "1", "HERMES_NONINTERACTIVE": "false"}
        with patch.object(setup, "_config_module", return_value=config):
            with patch.dict(setup.os.environ, launch_flags):
                with patch.object(
                    setup.subprocess, "run", return_value=Mock(returncode=0)
                ) as command:
                    setup._native_mcp_add(DEFAULT_MCP)
                self.assertEqual(setup.os.environ["HERMES_YOLO_MODE"], "1")
        child = command.call_args.kwargs["env"]
        for name in (*setup._UNATTENDED_ENV, "HERMES_YOLO_MODE"):
            self.assertEqual(child[name], "0")


class InteractiveBoundaryTests(unittest.TestCase):
    def assert_rejected_without_io(self, command, *, terminal, environment=None):
        with contextlib.ExitStack() as stack:
            output = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(patch.object(setup.sys.stdin, "isatty", return_value=terminal))
            stack.enter_context(
                patch.dict(
                    setup.os.environ,
                    {**dict.fromkeys(setup._UNATTENDED_ENV, "0"), **(environment or {})},
                )
            )
            profile = stack.enter_context(patch.object(setup, "_config_module"))
            catalog = stack.enter_context(patch.object(setup, "fetch_models"))
            secret = stack.enter_context(patch.object(setup, "_get_secret"))
            prompt = stack.enter_context(patch("builtins.input"))
            key_prompt = stack.enter_context(patch("getpass.getpass"))
            child = stack.enter_context(patch.object(setup.subprocess, "run"))
            result = setup.handle_command(argparse.Namespace(aixy_command=command))
        self.assertEqual(result, 1)
        self.assertIn("interactive terminal", output.getvalue())
        for mocked in (profile, catalog, secret, prompt, key_prompt, child):
            mocked.assert_not_called()

    def test_inference_setup_without_terminal_fails_before_any_io(self):
        self.assert_rejected_without_io("setup", terminal=False)

    def test_mcp_setup_without_terminal_fails_before_any_io(self):
        self.assert_rejected_without_io("mcp", terminal=False)

    def test_unattended_markers_are_rejected_even_with_a_terminal(self):
        for name in setup._UNATTENDED_ENV:
            for command in ("setup", "mcp"):
                with self.subTest(marker=name, command=command):
                    self.assert_rejected_without_io(command, terminal=True, environment={name: "1"})

    def test_native_mcp_without_terminal_cannot_spawn_a_child(self):
        with patch.object(setup.sys.stdin, "isatty", return_value=False):
            with patch.object(setup, "_config_module") as profile:
                with patch.object(setup.subprocess, "run") as child:
                    with self.assertRaisesRegex(setup.SetupError, "interactive terminal"):
                        setup._native_mcp_add(DEFAULT_MCP)
        profile.assert_not_called()
        child.assert_not_called()


class SetupBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.existing = {
            "model": {"provider": "other", "default": "other-model", "temperature": 0.2},
            "providers": {"other": {"api": "https://other.example/v1", "api_key": "${OTHER_KEY}"}},
            "mcp_servers": {"existing": {"url": "https://tools.example/mcp", "auth": "oauth"}},
            "display": {"compact": True},
        }
        self.config = FakeHermesConfig(self.existing)
        self.output = io.StringIO()
        self.catalog = [{"id": "aixy/coding", "context_length": 64000}]
        self.answers = {}
        self.prompts = []
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(setup.sys.stdin, "isatty", return_value=True))
        self.stack.enter_context(
            patch.dict(setup.os.environ, dict.fromkeys(setup._UNATTENDED_ENV, "0"))
        )
        self.stack.enter_context(contextlib.redirect_stdout(self.output))
        self.stack.enter_context(contextlib.redirect_stderr(self.output))
        self.stack.enter_context(patch.object(setup, "_config_module", return_value=self.config))
        self.secret_lookup = self.stack.enter_context(
            patch.object(setup, "_get_secret", return_value=None)
        )
        self.fetch = self.stack.enter_context(
            patch.object(setup, "fetch_models", return_value=self.catalog)
        )
        self.native_mcp = self.stack.enter_context(
            patch.object(setup, "_native_mcp_add", return_value=0)
        )
        self.secret_prompt = self.stack.enter_context(
            patch("getpass.getpass", return_value=FAKE_KEY)
        )
        self.stack.enter_context(patch("builtins.input", side_effect=self.answer))

    def answer(self, prompt):
        self.prompts.append(prompt)
        normalized = prompt.lower()
        for needle, answer in self.answers.items():
            if needle.lower() in normalized:
                return answer
        if "save" in normalized:
            return "y"
        if "context" in normalized:
            raise AssertionError("Setup must not ask the user for a context length")
        return ""

    def command(self, *args):
        parser = argparse.ArgumentParser()
        setup.setup_parser(parser)
        return setup.handle_command(parser.parse_args(args))

    def test_default_choice_configures_inference_without_contacting_mcp(self):
        self.assertEqual(self.command("setup", "--base-url", DEFAULT_BASE), 0)
        self.assertEqual(self.config.state["model"]["provider"], "aixy")
        self.assertEqual(self.config.state["model"]["default"], "aixy/coding")
        self.assertNotIn("context_length", self.config.state["model"])
        self.assertNotIn("context_length", self.config.state)
        self.assertFalse(any("context" in prompt.lower() for prompt in self.prompts))
        self.native_mcp.assert_not_called()
        self.assertTrue(any("mcp" in prompt.lower() for prompt in self.prompts))

    def test_preserves_unrelated_provider_mcp_and_settings(self):
        self.command("setup", "--base-url", DEFAULT_BASE)
        self.assertEqual(
            self.config.state["providers"]["other"], self.existing["providers"]["other"]
        )
        self.assertEqual(self.config.state["mcp_servers"], self.existing["mcp_servers"])
        self.assertEqual(self.config.state["display"], self.existing["display"])
        self.assertEqual(self.config.state["model"]["temperature"], 0.2)

    def test_secret_is_referenced_in_configuration_and_never_printed(self):
        self.command("setup", "--base-url", DEFAULT_BASE)
        self.assertNotIn(FAKE_KEY, self.output.getvalue())
        self.assertNotIn(FAKE_KEY, repr(self.config.state))
        self.assertEqual(self.config.state["providers"]["aixy"]["api_key"], "${AIXY_API_KEY}")
        self.config.save_env_value_secure.assert_called_once_with("AIXY_API_KEY", FAKE_KEY)

    def test_previous_model_routing_overrides_cannot_bypass_aixy(self):
        self.config.state["model"].update(
            {
                "base_url": "https://old.example/v1",
                "api_key": "${OLD_KEY}",
                "key_env": "OLD_KEY",
                "api_key_env": "OLD_KEY",
                "api_mode": "anthropic_messages",
            }
        )
        self.command("setup", "--base-url", DEFAULT_BASE)
        for key in ("base_url", "api_key", "key_env", "api_key_env"):
            self.assertFalse(self.config.state["model"].get(key))
        self.assertEqual(self.config.state["model"].get("api_mode"), "chat_completions")
        self.assertEqual(self.config.state["model"].get("openai_runtime"), "auto")
        self.assertEqual(self.config.state["model"]["provider"], "aixy")

    def test_replacing_provider_discards_stale_credential_and_transport_overrides(self):
        self.config.state["providers"]["aixy"] = {
            "api": DEFAULT_BASE,
            "api_key": "${OLD_KEY}",
            "key_cmd": "old-secret-command",
            "headers": {"Authorization": "Bearer old"},
            "transport": "anthropic_messages",
        }
        self.answers["replace"] = "y"
        self.command("setup", "--base-url", DEFAULT_BASE)
        self.assertEqual(
            self.config.state["providers"]["aixy"],
            {"api": DEFAULT_BASE, "api_key": "${AIXY_API_KEY}", "transport": "openai_chat"},
        )

    def test_saved_key_is_not_reused_when_changing_gateway(self):
        self.config.state["providers"]["aixy"] = {"api": DEFAULT_BASE}
        self.secret_lookup.return_value = FAKE_KEY
        self.secret_prompt.return_value = ""
        self.answers["replace"] = "y"
        self.command("setup", "--base-url", "https://another-gateway.example/v1")
        self.fetch.assert_not_called()
        self.config.save_env_value_secure.assert_not_called()
        self.config.save_config.assert_not_called()

    def test_existing_key_can_be_kept_for_the_same_gateway(self):
        self.config.state["providers"]["aixy"] = {"api": DEFAULT_BASE}
        self.secret_lookup.return_value = FAKE_KEY
        self.secret_prompt.return_value = ""
        self.answers["replace"] = "y"
        self.command("setup", "--base-url", DEFAULT_BASE)
        self.fetch.assert_called_once_with(DEFAULT_BASE, FAKE_KEY)
        self.config.save_env_value_secure.assert_not_called()
        self.assertEqual(self.config.state["model"]["provider"], "aixy")

    def test_mcp_opt_in_uses_custom_endpoint(self):
        custom_mcp = "https://gateway.example/aixy/mcp"
        self.answers["mcp"] = "y"
        self.command("setup", "--base-url", DEFAULT_BASE, "--mcp-url", custom_mcp)
        self.native_mcp.assert_called_once_with(custom_mcp)
        self.assertEqual(self.config.state["model"]["provider"], "aixy")

    def test_successful_optional_oauth_keeps_inference_and_other_connections(self):
        self.answers["mcp"] = "y"

        def connect(url):
            self.config.state["mcp_servers"]["aixy"] = {"url": url, "auth": "oauth"}

        self.native_mcp.side_effect = connect
        result = self.command("setup", "--base-url", DEFAULT_BASE, "--mcp-url", DEFAULT_MCP)
        self.assertEqual(result, 0)
        self.assertEqual(self.config.state["model"]["provider"], "aixy")
        self.assertEqual(
            self.config.state["mcp_servers"]["existing"],
            self.existing["mcp_servers"]["existing"],
        )
        self.assertIn("Aixy MCP configured", self.output.getvalue())

    def test_native_exit_without_saving_oauth_does_not_report_mcp_success(self):
        self.answers["mcp"] = "y"
        result = self.command("setup", "--base-url", DEFAULT_BASE, "--mcp-url", DEFAULT_MCP)
        self.assertEqual(result, 1)
        self.assertEqual(self.config.state["model"]["provider"], "aixy")
        self.assertNotIn("Aixy MCP configured", self.output.getvalue())
        self.assertIn("Retry with: hermes aixy mcp", self.output.getvalue())

    def test_existing_mcp_is_preserved_when_native_replacement_is_declined(self):
        self.config.state["mcp_servers"]["aixy"] = {
            "url": "https://existing.example/mcp",
            "auth": "oauth",
            "tools": {"include": ["read_only"]},
        }
        before = copy.deepcopy(self.config.state)
        self.assertEqual(self.command("mcp", "--url", DEFAULT_MCP), 0)
        self.assertEqual(self.config.state, before)
        self.config.save_config.assert_not_called()
        self.assertIn("Existing Aixy MCP settings kept", self.output.getvalue())

    def test_later_mcp_does_not_read_or_change_inference(self):
        self.command("mcp", "--url", DEFAULT_MCP)
        self.native_mcp.assert_called_once_with(DEFAULT_MCP)
        self.fetch.assert_not_called()
        self.secret_prompt.assert_not_called()
        self.secret_lookup.assert_not_called()
        self.config.save_env_value_secure.assert_not_called()
        self.config.save_config.assert_not_called()
        self.assertEqual(self.config.state, self.existing)

    def test_custom_base_is_used_for_both_catalog_and_provider(self):
        self.command("setup", "--base-url", "https://gateway.example/aixy")
        self.fetch.assert_called_once_with("https://gateway.example/aixy/v1", FAKE_KEY)
        self.assertEqual(
            self.config.state["providers"]["aixy"]["api"], "https://gateway.example/aixy/v1"
        )

    def test_insecure_base_fails_before_secret_or_network(self):
        self.command("setup", "--base-url", "http://gateway.example/v1")
        self.fetch.assert_not_called()
        self.secret_prompt.assert_not_called()
        self.config.save_env_value_secure.assert_not_called()
        self.config.save_config.assert_not_called()

    def test_insecure_mcp_endpoint_never_reaches_native_oauth(self):
        self.command("mcp", "--url", "http://gateway.example/mcp")
        self.native_mcp.assert_not_called()

    def test_unknown_context_uses_automatic_resolution_without_a_prompt(self):
        self.fetch.return_value = [{"id": "aixy/coding"}]
        self.assertEqual(self.command("setup", "--base-url", DEFAULT_BASE), 0)
        self.assertNotIn("context_length", self.config.state["model"])
        self.assertFalse(any("context" in prompt.lower() for prompt in self.prompts))

    def test_advertised_context_is_informational_and_does_not_pin_later_models(self):
        self.fetch.return_value = [{"id": "openai/test-model", "context_length": 96000}]
        self.assertEqual(self.command("setup", "--base-url", DEFAULT_BASE), 0)
        self.assertIn("96000", self.output.getvalue())
        self.assertEqual(self.config.state["model"]["default"], "openai/test-model")
        self.assertNotIn("context_length", self.config.state["model"])
        self.assertNotIn("context_length", self.config.state)
        self.assertFalse(any("context" in prompt.lower() for prompt in self.prompts))

    def test_malformed_catalog_context_never_requires_input_or_persists_a_limit(self):
        for value in (None, "128000", 0, -1, True, 1.5):
            with self.subTest(context=value):
                self.config.state = copy.deepcopy(self.existing)
                self.prompts.clear()
                self.fetch.return_value = setup.parse_catalog(
                    {"data": [{"id": "aixy/coding", "context_length": value}]}
                )
                self.assertEqual(self.command("setup", "--base-url", DEFAULT_BASE), 0)
                self.assertNotIn("context_length", self.config.state["model"])
                self.assertFalse(any("context" in prompt.lower() for prompt in self.prompts))

    def test_manually_selected_route_needs_no_context_input(self):
        self.answers["model id"] = "aixy/manual-route"
        self.assertEqual(self.command("setup", "--base-url", DEFAULT_BASE), 0)
        self.assertEqual(self.config.state["model"]["default"], "aixy/manual-route")
        self.assertNotIn("context_length", self.config.state["model"])
        self.assertFalse(any("context" in prompt.lower() for prompt in self.prompts))

    def test_empty_catalog_allows_an_exact_model_without_context_input(self):
        self.fetch.return_value = []
        self.answers["model id"] = "openai/test-model"
        self.assertEqual(self.command("setup", "--base-url", DEFAULT_BASE), 0)
        self.assertEqual(self.config.state["model"]["default"], "openai/test-model")
        self.assertNotIn("context_length", self.config.state["model"])
        self.assertFalse(any("context" in prompt.lower() for prompt in self.prompts))
        self.assertNotIn("verified context limit", self.output.getvalue())

    def test_automatic_setup_removes_old_global_and_provider_context_overrides(self):
        self.config.state["context_length"] = 8192
        self.config.state["model"]["context_length"] = 16384
        self.config.state["providers"]["aixy"] = {
            "api": DEFAULT_BASE,
            "context_length": 32768,
            "max_context_length": 65536,
        }
        self.answers["replace"] = "y"
        self.assertEqual(self.command("setup", "--base-url", DEFAULT_BASE), 0)
        self.assertNotIn("context_length", self.config.state)
        self.assertNotIn("context_length", self.config.state["model"])
        self.assertEqual(
            self.config.state["providers"]["aixy"],
            {"api": DEFAULT_BASE, "api_key": "${AIXY_API_KEY}", "transport": "openai_chat"},
        )
        self.assertEqual(self.config.state["display"], self.existing["display"])
        self.assertEqual(self.config.state["model"]["temperature"], 0.2)

    def test_advanced_explicit_context_override_is_saved_without_an_extra_prompt(self):
        self.config.state["context_length"] = 8192
        self.config.state["model"]["context_length"] = 16384
        self.assertEqual(
            self.command("setup", "--base-url", DEFAULT_BASE, "--context-length", "96000"), 0
        )
        self.assertEqual(self.config.state["model"]["context_length"], 96000)
        self.assertNotIn("context_length", self.config.state)
        self.assertFalse(any("context" in prompt.lower() for prompt in self.prompts))

    def test_invalid_context_override_is_rejected_by_argparse_before_any_io(self):
        for value in ("0", "-1", "not-a-number", "1.5"):
            with self.subTest(context=value):
                with self.assertRaises(SystemExit) as caught:
                    self.command("setup", "--context-length", value)
                self.assertEqual(caught.exception.code, 2)
        self.config.require_readable_config_before_write.assert_not_called()
        self.config.save_config.assert_not_called()
        self.config.save_env_value_secure.assert_not_called()
        self.secret_lookup.assert_not_called()
        self.secret_prompt.assert_not_called()
        self.fetch.assert_not_called()
        self.native_mcp.assert_not_called()
        self.assertEqual(self.prompts, [])

    def test_declining_save_has_no_persistent_effect(self):
        self.answers["save"] = ""
        self.command("setup", "--base-url", DEFAULT_BASE)
        self.config.save_config.assert_not_called()
        self.config.save_env_value_secure.assert_not_called()
        self.native_mcp.assert_not_called()
        self.assertEqual(self.config.state, self.existing)

    def test_conflicting_aixy_provider_is_not_overwritten_by_default(self):
        self.config.state["providers"]["aixy"] = {"api": "https://existing.example/v1"}
        before = copy.deepcopy(self.config.state)
        self.command("setup", "--base-url", DEFAULT_BASE)
        self.config.save_config.assert_not_called()
        self.config.save_env_value_secure.assert_not_called()
        self.secret_prompt.assert_not_called()
        self.fetch.assert_not_called()
        self.assertEqual(self.config.state, before)

    def test_unreadable_config_fails_before_any_credential_write(self):
        self.config.require_readable_config_before_write.side_effect = ValueError("invalid YAML")
        self.command("setup", "--base-url", DEFAULT_BASE)
        self.config.save_config.assert_not_called()
        self.config.save_env_value_secure.assert_not_called()
        self.native_mcp.assert_not_called()
        self.secret_prompt.assert_not_called()

    def test_managed_profile_is_not_modified(self):
        self.config.is_managed.return_value = True
        self.command("setup", "--base-url", DEFAULT_BASE)
        self.config.save_config.assert_not_called()
        self.config.save_env_value_secure.assert_not_called()
        self.secret_prompt.assert_not_called()
        self.fetch.assert_not_called()

    def test_catalog_failure_does_not_persist_a_half_configuration(self):
        self.fetch.side_effect = RuntimeError("catalog unavailable")
        self.command("setup", "--base-url", DEFAULT_BASE)
        self.config.save_config.assert_not_called()
        self.config.save_env_value_secure.assert_not_called()
        self.native_mcp.assert_not_called()
        self.assertEqual(self.config.state, self.existing)

    def test_failed_secure_secret_save_never_selects_the_new_provider(self):
        self.config.save_env_value_secure.return_value = {"success": False}
        self.command("setup", "--base-url", DEFAULT_BASE)
        self.config.save_config.assert_not_called()
        self.native_mcp.assert_not_called()
        self.assertEqual(self.config.state, self.existing)
        self.assertNotIn(FAKE_KEY, self.output.getvalue())

    def test_oauth_failure_preserves_saved_inference(self):
        self.answers["mcp"] = "y"
        self.native_mcp.side_effect = RuntimeError("OAuth was denied")
        self.command("setup", "--base-url", DEFAULT_BASE, "--mcp-url", DEFAULT_MCP)
        self.assertEqual(self.config.state["model"]["provider"], "aixy")
        self.assertEqual(self.config.state["model"]["default"], "aixy/coding")
        self.assertNotIn(FAKE_KEY, self.output.getvalue())


class ModelSelectionTests(unittest.TestCase):
    def test_choosing_by_list_number_returns_only_the_exact_model_id(self):
        catalog = [
            {"id": "openai/test-model", "context_length": 96000},
            {"id": "aixy/route"},
        ]
        with contextlib.redirect_stdout(io.StringIO()):
            with patch("builtins.input", return_value="2") as prompt:
                selected = setup._choose_model(catalog, "", FAKE_KEY)
        self.assertEqual(selected, "aixy/route")
        prompt.assert_called_once()
        self.assertNotIn("context", prompt.call_args.args[0].lower())


class EndpointValidationTests(unittest.TestCase):
    def test_normalizes_inference_without_losing_deployment_prefix(self):
        examples = {
            "https://gateway.example.com": "https://gateway.example.com/v1",
            "https://gateway.example.com/": "https://gateway.example.com/v1",
            "https://gateway.example.com/aixy": "https://gateway.example.com/aixy/v1",
            "https://gateway.example.com/aixy/v1/": "https://gateway.example.com/aixy/v1",
        }
        for source, expected in examples.items():
            with self.subTest(source=source):
                self.assertEqual(setup.validate_url(source, inference=True), expected)

    def test_preserves_custom_mcp_path(self):
        self.assertEqual(
            setup.validate_url("https://gateway.example.com/aixy/mcp"),
            "https://gateway.example.com/aixy/mcp",
        )

    def test_rejects_untrusted_endpoint_forms(self):
        for value in (
            "http://gateway.example.com/v1",
            "ftp://gateway.example.com/v1",
            "https://user:password@gateway.example.com/v1",
            "https://gateway.example.com/v1?key=secret",
            "https://gateway.example.com/v1#secret",
            "https://gateway.example.com:bad/v1",
            "https:///v1",
            "gateway.example.com/v1",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                setup.validate_url(value, inference=True)


class CatalogTests(unittest.TestCase):
    def test_keeps_qualified_models_and_virtual_routes(self):
        models = setup.parse_catalog(
            {
                "data": [
                    {"id": "openai/test-model", "context_length": 128000},
                    {"id": "aixy/coding"},
                ]
            }
        )
        self.assertEqual([model["id"] for model in models], ["openai/test-model", "aixy/coding"])
        self.assertEqual(models[0].get("context_length"), 128000)
        self.assertFalse(models[1].get("context_length"))

    def test_never_invents_context_for_malformed_metadata(self):
        for context in (None, "128000", 0, -1, True, 1.5):
            with self.subTest(context=context):
                models = setup.parse_catalog(
                    {"data": [{"id": "aixy/route", "context_length": context}]}
                )
                self.assertEqual(models[0]["id"], "aixy/route")
                self.assertFalse(models[0].get("context_length"))

    def test_skips_duplicate_and_invalid_model_ids(self):
        models = setup.parse_catalog(
            {
                "data": [
                    {"id": "aixy/coding"},
                    {"id": "aixy/coding"},
                    {"id": "bad\nmodel"},
                    {"id": ""},
                    {"id": None},
                    "unexpected",
                ]
            }
        )
        self.assertEqual(models, [{"id": "aixy/coding"}])


class CatalogRequestTests(unittest.TestCase):
    def test_discovery_uses_authenticated_get_and_bounded_response(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps({"data": [{"id": "aixy/coding"}]}).encode()
        opener = Mock()
        opener.open.return_value = response
        with patch.object(setup.urllib.request, "build_opener", return_value=opener):
            models = setup.fetch_models("https://gateway.example/aixy/v1", FAKE_KEY)
        self.assertEqual(models, [{"id": "aixy/coding"}])
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://gateway.example/aixy/v1/models")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + FAKE_KEY)
        self.assertGreater(opener.open.call_args.kwargs["timeout"], 0)
        self.assertLessEqual(opener.open.call_args.kwargs["timeout"], 30)
        response.read.assert_called_once_with(setup.MAX_CATALOG_BYTES + 1)

    def test_discovery_error_does_not_expose_credential_or_server_body(self):
        reflected = FAKE_KEY + " private server response"
        error = urllib.error.HTTPError(DEFAULT_BASE + "/models", 403, reflected, {}, None)
        opener = Mock()
        opener.open.side_effect = error
        with patch.object(setup.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(setup.SetupError) as caught:
                setup.fetch_models(DEFAULT_BASE, FAKE_KEY)
        self.assertNotIn(FAKE_KEY, str(caught.exception))
        self.assertNotIn("private server response", str(caught.exception))

    def test_redirect_handler_refuses_to_forward_credentials(self):
        captured = []

        def build_opener(*handlers):
            captured.extend(handlers)
            opener = Mock()
            opener.open.side_effect = setup.SetupError("stopped")
            return opener

        with patch.object(setup.urllib.request, "build_opener", side_effect=build_opener):
            with self.assertRaises(setup.SetupError):
                setup.fetch_models(DEFAULT_BASE, FAKE_KEY)
        request = setup.urllib.request.Request(DEFAULT_BASE + "/models")
        self.assertEqual(len(captured), 1)
        with self.assertRaises(setup.SetupError):
            captured[0].redirect_request(
                request, None, 302, "Found", {}, "https://different.example/v1/models"
            )


if __name__ == "__main__":
    unittest.main()
