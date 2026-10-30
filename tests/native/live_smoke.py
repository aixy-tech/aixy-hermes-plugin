"""Manually authorized, bounded inference smoke; importing this module has no effects."""

from __future__ import annotations

import argparse
import contextlib
import getpass
import hashlib
import importlib
import importlib.util
import io
import json
import logging
import os
import socket
import stat
import sys
import tempfile
import threading
import uuid
import warnings
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

PHASES = (
    "tool",
    "tool_result",
    "stream",
    "auxiliary",
    "second_conversation",
    "cache_prime",
    "cache_reuse",
)
PLUGIN_ROOT = Path(__file__).resolve().parents[2]
HEADER = "X-Aixy-Session-ID"
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "synthetic_add",
            "description": "Add two synthetic integers locally.",
            "parameters": {
                "type": "object",
                "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                "required": ["a", "b"],
                "additionalProperties": False,
            },
        },
    }
]


class SmokeFailure(RuntimeError):
    """Only a fixed, non-sensitive failure code may be exposed to the caller."""


@dataclass(frozen=True)
class Settings:
    base_url: str
    model: str
    context_length: int
    max_input_usd_per_million: Decimal
    max_output_usd_per_million: Decimal
    cache_min_tokens: int = 2048
    output_limit: int = 256
    reasoning_effort: str = "low"

    @classmethod
    def read(cls, path):
        raw = json.loads(Path(path).read_text())
        allowed = set(cls.__dataclass_fields__)
        if not isinstance(raw, dict) or set(raw) - allowed:
            raise SmokeFailure("invalid_settings")
        for field in ("max_input_usd_per_million", "max_output_usd_per_million"):
            raw[field] = Decimal(str(raw[field]))
        result = cls(**raw)
        result.validate()
        return result

    def validate(self):
        url = urlsplit(self.base_url)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or not url.path.endswith("/v1")
            or self.base_url.endswith("/")
        ):
            raise SmokeFailure("invalid_gateway")
        if not self.model or not self.model.isascii() or not self.model.isprintable():
            raise SmokeFailure("invalid_model")
        if not isinstance(self.context_length, int) or self.context_length < 8192:
            raise SmokeFailure("invalid_context")
        if not 1 <= self.output_limit <= 256 or not 1 <= self.cache_min_tokens <= 8192:
            raise SmokeFailure("invalid_token_limits")
        if self.reasoning_effort != "low":
            raise SmokeFailure("invalid_reasoning_effort")
        if any(
            not price.is_finite() or price <= 0
            for price in (
                self.max_input_usd_per_million,
                self.max_output_usd_per_million,
            )
        ):
            raise SmokeFailure("unknown_prices")

    def bound(self, input_tokens, output_tokens):
        return (
            Decimal(input_tokens) * self.max_input_usd_per_million
            + Decimal(output_tokens) * self.max_output_usd_per_million
        ) / Decimal(1_000_000)


class RequestGate:
    """One atomic send permit per named phase, shared with Hermes' request workers."""

    def __init__(self, settings, run_id, responder=None):
        self.settings, self.run_id, self.responder = settings, run_id, responder
        self.rows, self.blocked, self.permit = [], 0, None
        self.lock, self.local = threading.Lock(), threading.local()
        self.reserved_cost, self.actual_cost = Decimal(0), Decimal(0)

    def arm(self, phase, session):
        with self.lock:
            if self.blocked or self.permit is not None or phase != PHASES[len(self.rows)]:
                raise SmokeFailure("invalid_phase")
            self.permit = (phase, session)

    def reject(self, code):
        self.blocked += 1
        self.permit = None
        raise SmokeFailure(code)

    def send(self, request, original):
        with self.lock:
            if self.permit is None or len(self.rows) >= 8:
                self.reject("unexpected_request")
            phase, session = self.permit
            self.permit = None
            try:
                body = json.loads(request.content)
            except (ValueError, UnicodeError):
                self.reject("invalid_request_json")
            expected = self.settings.base_url + "/chat/completions"
            caps = [body[name] for name in ("max_completion_tokens", "max_tokens") if name in body]
            cap = max(caps) if caps and all(isinstance(value, int) for value in caps) else None
            if (
                str(request.url) != expected
                or request.method != "POST"
                or body.get("model") != self.settings.model
                or body.get("reasoning_effort") != self.settings.reasoning_effort
                or request.headers.get(HEADER) != session
                or body.get("n", 1) != 1
                or not isinstance(cap, int)
                or any(
                    isinstance(value, bool) or not 1 <= value <= self.settings.output_limit
                    for value in caps
                )
                or not 1 <= cap <= self.settings.output_limit
                or not request.content.isascii()
                or len(request.content) > 8192
            ):
                self.reject("request_outside_limits")
            reserve = self.settings.bound(8192, cap)
            if self.reserved_cost + reserve > Decimal("1"):
                self.reject("cost_limit")
            self.reserved_cost += reserve
            request_id = f"hermes-smoke-{self.run_id}-{phase}"
            request.headers["X-Request-ID"] = request_id
            row = {
                "phase": phase,
                "request_id": request_id,
                "session_id": session,
                "request_bytes": len(request.content),
                "stream": body.get("stream", False),
                "sent": True,
                "sse_done": False,
            }
            self.rows.append(row)  # A failed transport still consumes its send.
        self.local.allowed = self.responder is None
        try:
            response = self.responder(request) if self.responder else original()
        finally:
            self.local.allowed = False
        row["http_status"] = response.status_code
        if response.status_code != 200:
            raise SmokeFailure("http_failure")
        if row["stream"]:
            import httpx

            inner = response.stream

            class ObservedStream(httpx.SyncByteStream):
                def __iter__(self):
                    tail = b""
                    for chunk in inner:
                        data = tail + chunk
                        row["sse_done"] = row["sse_done"] or b"data: [DONE]" in data
                        tail = data[-32:]
                        yield chunk

                def close(self):
                    inner.close()

            response.stream = ObservedStream()
        return response

    def usage(self, response):
        usage = getattr(response, "usage", None)
        prompt = getattr(usage, "prompt_tokens", None)
        output = getattr(usage, "completion_tokens", None)
        if (
            not isinstance(prompt, int)
            or not isinstance(output, int)
            or prompt < 0
            or output < 0
            or prompt > 8192
            or output > self.settings.output_limit
        ):
            raise SmokeFailure("unknown_or_excess_usage")
        details = getattr(usage, "prompt_tokens_details", None)
        cached = getattr(details, "cached_tokens", 0) or 0
        self.rows[-1].update(input_tokens=prompt, output_tokens=output, cached_tokens=cached)
        self.actual_cost += self.settings.bound(prompt, output)
        if self.actual_cost > Decimal("1"):
            raise SmokeFailure("cost_limit")


def run_smoke(settings, source, key, *, execute=False, responder=None):
    """The responder hook is for offline tests; live sends require explicit execute=True."""
    settings.validate()
    if not execute and responder is None:
        raise SmokeFailure("execution_not_authorized")
    if not key or not key.isascii() or any(char.isspace() for char in key):
        raise SmokeFailure("invalid_credential")
    source = Path(source).resolve()
    if not (source / "hermes_cli" / "config.py").is_file():
        raise SmokeFailure("invalid_hermes_source")
    run_id = uuid.uuid4().hex[:16]
    gate = RequestGate(settings, run_id, responder)
    report = {"run_id": run_id, "model": settings.model, "status": "failed", "requests": gate.rows}
    try:
        with contextlib.ExitStack() as stack:
            profile = stack.enter_context(tempfile.TemporaryDirectory(prefix="aixy-live-smoke-"))
            environment = {
                name: os.environ[name] for name in ("HOME", "PATH") if name in os.environ
            }
            environment.update(
                HERMES_HOME=profile,
                HERMES_RUNTIME_DIR=str(Path(profile) / "runtime"),
                HERMES_NONINTERACTIVE="1",
                HERMES_DISABLE_LAZY_INSTALLS="1",
                HERMES_ENABLE_PROJECT_PLUGINS="0",
                HERMES_STREAM_RETRIES="0",
                PYTHONDONTWRITEBYTECODE="1",
            )
            stack.enter_context(patch.dict(os.environ, environment, clear=True))
            stack.enter_context(patch.object(sys, "path", [str(source), *sys.path]))
            stack.enter_context(patch.object(sys, "dont_write_bytecode", True))
            previous_logging = logging.root.manager.disable
            logging.disable(logging.CRITICAL)
            stack.callback(logging.disable, previous_logging)
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            import httpx

            original_transport = httpx.HTTPTransport.handle_request
            stack.enter_context(
                patch.object(
                    httpx.HTTPTransport,
                    "handle_request",
                    lambda transport, request: gate.send(
                        request, lambda: original_transport(transport, request)
                    ),
                )
            )

            async def deny_async(*_args, **_kwargs):
                gate.reject("unexpected_async_request")

            stack.enter_context(
                patch.object(httpx.AsyncHTTPTransport, "handle_async_request", deny_async)
            )
            for target, name in (
                (socket, "getaddrinfo"),
                (socket, "create_connection"),
                (socket.socket, "connect"),
                (socket.socket, "connect_ex"),
            ):
                original = getattr(target, name)

                def checked(*args, _original=original, **kwargs):
                    if not getattr(gate.local, "allowed", False):
                        gate.reject("unexpected_network")
                    return _original(*args, **kwargs)

                stack.enter_context(patch.object(target, name, checked))
            for name in ("restore_interrupted_pull", "recover_if_needed"):
                stack.enter_context(patch(f"hermes_cli._early_recovery.{name}", return_value=False))
            stack.enter_context(patch("hermes_cli.env_loader.load_hermes_dotenv", return_value=[]))
            config = importlib.import_module("hermes_cli.config")
            if not Path(config.__file__).resolve().is_relative_to(source):
                raise SmokeFailure("wrong_hermes_source")
            spec = importlib.util.spec_from_file_location(
                "aixy_live_setup", PLUGIN_ROOT / "aixy_setup.py"
            )
            setup = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(setup)
            raw = setup._inference_config(
                {
                    "agent": {
                        "environment_probe": False,
                        "api_max_retries": 1,
                        "auto_recovery_cycles": 0,
                    },
                    "compression": {"enabled": False},
                    "auxiliary": {"transient_retries": 0},
                    "telemetry": {"enabled": False},
                },
                settings.base_url,
                settings.model,
                settings.context_length,
            )
            config.save_config(raw, merge_existing=False)
            if not config.save_env_value_secure("AIXY_API_KEY", key).get("success"):
                raise SmokeFailure("credential_store_failed")
            metadata = importlib.import_module("agent.model_metadata")
            stack.enter_context(
                patch.object(
                    metadata,
                    "fetch_endpoint_model_metadata",
                    return_value={
                        settings.model: {
                            "id": settings.model,
                            "context_length": settings.context_length,
                        }
                    },
                )
            )
            stack.enter_context(patch.object(metadata, "fetch_model_metadata", return_value={}))
            stack.enter_context(patch("agent.models_dev.fetch_models_dev", return_value={}))
            Agent = importlib.import_module("run_agent").AIAgent
            stack.enter_context(patch.object(Agent, "_safe_print", lambda *_a, **_kw: None))
            aux = importlib.import_module("agent.auxiliary_client")
            stack.callback(aux.shutdown_cached_clients)
            tags = importlib.import_module("agent.portal_tags")
            for setter, reset in (
                (tags.set_conversation_context, tags.reset_conversation_context),
                (tags.set_affinity_scope, tags.reset_affinity_scope),
            ):
                stack.callback(reset, setter(None))
            runtime = importlib.import_module(
                "hermes_cli.runtime_provider"
            ).resolve_runtime_provider(requested="aixy", target_model=settings.model)
            if (
                runtime["base_url"] != settings.base_url
                or runtime["api_mode"] != "chat_completions"
            ):
                raise SmokeFailure("unexpected_route")
            streamed = []

            def agent(suffix):
                result = Agent(
                    api_key=key,
                    base_url=settings.base_url,
                    provider="aixy",
                    model=settings.model,
                    api_mode="chat_completions",
                    session_id=f"hermes-smoke-{run_id}-{suffix}",
                    max_tokens=settings.output_limit,
                    max_iterations=2,
                    enabled_toolsets=[],
                    skip_memory=True,
                    skip_context_files=True,
                    skip_background_review=True,
                    quiet_mode=True,
                    fallback_model={},
                    reasoning_config={"enabled": True, "effort": settings.reasoning_effort},
                    stream_delta_callback=streamed.append,
                )
                stack.callback(result.close)
                return result

            primary, second = agent("A"), agent("B")

            def main(phase, current, messages, tools=None, stream=False, extra=None):
                gate.arm(phase, current.session_id)
                kwargs = current._build_api_kwargs(messages, tools_for_api=tools or [])
                kwargs.update(extra or {})
                current._reset_stream_delivery_tracking()
                response = (
                    current._interruptible_streaming_api_call(kwargs)
                    if stream
                    else current._interruptible_api_call(kwargs)
                )
                gate.usage(response)
                if gate.blocked:
                    raise SmokeFailure("hidden_retry_blocked")
                return response

            messages = [{"role": "user", "content": "Call synthetic_add once with a=2 and b=3."}]
            response = main(
                "tool",
                primary,
                messages,
                TOOLS,
                extra={
                    "tool_choice": {"type": "function", "function": {"name": "synthetic_add"}},
                    "parallel_tool_calls": False,
                },
            )
            normalized = primary._get_transport().normalize_response(response)
            calls = normalized.tool_calls or []
            if (
                len(calls) != 1
                or calls[0].name != "synthetic_add"
                or not calls[0].id
                or json.loads(calls[0].arguments) != {"a": 2, "b": 3}
            ):
                raise SmokeFailure("tool_contract_failed")
            tool = calls[0]
            history = [
                *messages,
                primary._build_assistant_message(
                    response.choices[0].message, response.choices[0].finish_reason
                ),
                {"role": "tool", "tool_call_id": tool.id, "content": str(2 + 3)},
                {"role": "user", "content": "Reply with the result only."},
            ]
            final = main("tool_result", primary, history)
            if "5" not in (final.choices[0].message.content or ""):
                raise SmokeFailure("tool_result_failed")
            stream_response = main(
                "stream",
                primary,
                [{"role": "user", "content": "Reply only SYNTHETIC_OK."}],
                stream=True,
            )
            if (
                not streamed
                or not gate.rows[-1]["sse_done"]
                or primary._get_transport().normalize_response(stream_response).finish_reason
                != "stop"
            ):
                raise SmokeFailure("stream_incomplete")
            gate.arm("auxiliary", primary.session_id)
            response = aux.call_llm(
                task="title_generation",
                provider="aixy",
                model=settings.model,
                base_url=settings.base_url,
                api_key=key,
                api_mode="chat_completions",
                main_runtime={
                    "provider": "aixy",
                    "model": settings.model,
                    "base_url": settings.base_url,
                    "api_key": key,
                    "api_mode": "chat_completions",
                    "session_id": primary.session_id,
                },
                messages=[
                    {"role": "user", "content": "Give a two-word title for a synthetic smoke test."}
                ],
                max_tokens=settings.output_limit,
                # Generic custom routes omit auxiliary max_tokens; use the supported
                # request-body override to keep this smoke explicitly capped on the wire.
                extra_body=primary._max_tokens_param(settings.output_limit),
                timeout=45,
                reasoning_config={"enabled": True, "effort": settings.reasoning_effort},
            )
            gate.usage(response)
            if gate.blocked:
                raise SmokeFailure("hidden_retry_blocked")
            main(
                "second_conversation",
                second,
                [{"role": "user", "content": "Reply only SECOND_OK."}],
            )
            prefix = "\n".join(
                hashlib.sha256(f"aixy-smoke-{run_id}-{i}".encode()).hexdigest() for i in range(96)
            )
            cache_messages = [
                {"role": "system", "content": "Synthetic inert cache prefix:\n" + prefix},
                {"role": "user", "content": "Do not interpret the prefix. Reply only CACHE_OK."},
            ]
            main("cache_prime", primary, cache_messages)
            if gate.rows[-1]["input_tokens"] < settings.cache_min_tokens:
                raise SmokeFailure("cache_threshold_not_reached")
            main("cache_reuse", primary, cache_messages)
            if gate.rows[-1]["cached_tokens"] <= 0:
                raise SmokeFailure("cache_hit_not_observed")
            report["status"] = "passed"
    except Exception as exc:
        code = exc.args[0] if isinstance(exc, SmokeFailure) else "native_client_failure"
        report["failure"] = code
        report["failure_type"] = type(exc).__name__
        if code in {"cache_threshold_not_reached", "cache_hit_not_observed"}:
            report["status"] = "inconclusive"
    billing_complete = all("input_tokens" in row for row in gate.rows)
    report.update(
        blocked_requests=gate.blocked,
        sent_requests=len(gate.rows),
        billing_complete=billing_complete,
        upper_cost_usd=str(gate.actual_cost) if billing_complete else None,
        reserved_upper_cost_usd=str(gate.reserved_cost),
    )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", required=True, help="Non-secret settings JSON; prices must be verified"
    )
    parser.add_argument("--report", help="Sanitized result JSON path")
    parser.add_argument(
        "--key-stdin", action="store_true", help="Read key with hidden TTY prompt or from pipe"
    )
    parser.add_argument(
        "--execute", action="store_true", help="Requires separate explicit authorization"
    )
    parser.add_argument(
        "--preflight-only", action="store_true", help="Validate settings offline, without a key"
    )
    args = parser.parse_args()
    try:
        settings = Settings.read(args.config)
        source = os.environ.get("AIXY_HERMES_SOURCE", "")
        if not source or not (Path(source) / "hermes_cli" / "config.py").is_file():
            raise SmokeFailure("invalid_hermes_source")
        if args.report and (Path(args.report).exists() or not Path(args.report).parent.is_dir()):
            raise SmokeFailure("invalid_report_destination")
        worst_case = settings.bound(8192, settings.output_limit) * len(PHASES)
        if worst_case > Decimal("1"):
            raise SmokeFailure("cost_limit")
        if args.preflight_only:
            result = {
                "status": "offline_preflight",
                "phases": PHASES,
                "maximum_sends": 8,
                "nominal_sends": len(PHASES),
                "maximum_request_bytes": 8192,
                "maximum_output_tokens": settings.output_limit,
                "worst_case_usd": str(worst_case),
            }
        else:
            if not args.execute or not args.key_stdin:
                raise SmokeFailure("execution_not_authorized")
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                key = (
                    getpass.getpass("Disposable project key: ")
                    if sys.stdin.isatty()
                    else sys.stdin.readline().strip()
                )
            result = run_smoke(settings, source, key, execute=True)
    except Exception as exc:
        result = {
            "status": "failed",
            "failure": exc.args[0] if isinstance(exc, SmokeFailure) else "invalid_configuration",
        }
    encoded = json.dumps(result, indent=2)
    if args.report:
        try:
            descriptor = os.open(
                args.report, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR
            )
            with os.fdopen(descriptor, "w") as stream:
                stream.write(encoded + "\n")
        except OSError:
            result["report_write_failed"] = True
            encoded = json.dumps(result, indent=2)
    print(encoded)
    return 0 if result["status"] in {"passed", "offline_preflight"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
