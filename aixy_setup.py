"""Interactive, profile-scoped Aixy setup using Hermes' own credential writers."""

from __future__ import annotations

import argparse
import copy
import getpass
import ipaddress
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import warnings
from pathlib import Path

DEFAULT_BASE_URL = "https://api.aixy-gateway.com/v1"
DEFAULT_MCP_URL = "https://api.aixy-gateway.com/mcp"
API_KEY_ENV = "AIXY_API_KEY"
API_KEY_REF = "${AIXY_API_KEY}"
MAX_CATALOG_BYTES = 4 * 1024 * 1024
_MODEL_ROUTE_KEYS = {"base_url", "api_key", "key_env", "api_key_env", "api_mode"}
_UNATTENDED_ENV = ("HERMES_NONINTERACTIVE", "HERMES_SINGLE_QUERY_SESSION", "HERMES_CRON_SESSION")


class SetupError(ValueError):
    """An actionable message that contains no credential or server response text."""


def _require_interactive_terminal():
    """Reject unattended setup before reading the profile, secrets, or network."""
    unattended = any(
        os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}
        for name in _UNATTENDED_ENV
    )
    if unattended or sys.stdin is None or not sys.stdin.isatty():
        raise SetupError(
            "Aixy setup requires an interactive terminal. "
            "Run hermes aixy setup or hermes aixy mcp directly in your terminal."
        )


def setup_parser(subparser):
    """Register arguments without accessing the filesystem or active profile."""
    commands = subparser.add_subparsers(dest="aixy_command")
    setup = commands.add_parser("setup", help="Configure inference, then optionally connect MCP")
    setup.add_argument("--base-url", help="Aixy inference base URL (HTTPS, normally ending in /v1)")
    setup.add_argument("--mcp-url", help="Independent Aixy MCP endpoint URL for optional OAuth")
    mcp = commands.add_parser("mcp", help="Connect Aixy MCP through Hermes OAuth at any time")
    mcp.add_argument("--url", help="Aixy MCP endpoint URL")
    subparser.set_defaults(func=handle_command)


def _config_module():
    from hermes_cli import config

    return config


def _get_secret(name):
    from agent.secret_scope import get_secret

    return get_secret(name)


def _writable_config():
    config = _config_module()
    if config.is_managed():
        raise SetupError(
            "This Hermes profile is managed. Configure Aixy through its managed configuration."
        )
    try:
        raw = config.require_readable_config_before_write()
    except Exception:
        raise SetupError(
            "Hermes configuration cannot be read safely. Repair config.yaml and try again."
        ) from None
    if not isinstance(raw, dict):
        raise SetupError(
            "Hermes configuration must be a mapping. Repair config.yaml and try again."
        )
    for section in ("providers", "mcp_servers"):
        if section in raw and not isinstance(raw[section], dict):
            raise SetupError(
                "Hermes provider and MCP sections must be mappings. "
                "Repair config.yaml and try again."
            )
    if "model" in raw and not isinstance(raw["model"], (dict, str)):
        raise SetupError(
            "Hermes model settings have an unsupported shape. Repair config.yaml and try again."
        )
    if "custom_providers" in raw and not isinstance(raw["custom_providers"], list):
        raise SetupError(
            "Hermes legacy providers must be a list. Repair config.yaml and try again."
        )
    return config, copy.deepcopy(raw)


def validate_url(value, *, inference=False):
    """Require a credential-free HTTPS endpoint, allowing HTTP only on loopback.

    A deployment prefix is preserved. Inference base URLs get a final /v1 when
    omitted; MCP endpoints are kept independent and are never derived from it.
    """
    if (
        not isinstance(value, str)
        or not value
        or any(ord(char) <= 32 or ord(char) == 127 for char in value)
    ):
        raise SetupError("Enter a valid endpoint URL without whitespace or control characters.")
    try:
        parts = urllib.parse.urlsplit(value)
        host = parts.hostname
        port = parts.port
    except ValueError:
        raise SetupError("Enter a valid endpoint URL and port.") from None
    if not host or parts.scheme not in {"https", "http"}:
        raise SetupError("Endpoints must use HTTPS; HTTP is allowed only for loopback development.")
    if parts.username is not None or parts.password is not None or "?" in value or "#" in value:
        raise SetupError(
            "Endpoint URLs must not contain credentials, query parameters, or fragments."
        )
    if "%" in host or "\\" in value or port == 0:
        raise SetupError("Enter a valid endpoint hostname and port.")
    try:
        loopback = host.lower() == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host.lower() == "localhost"
    if parts.scheme == "http" and not loopback:
        raise SetupError("Remote endpoints require HTTPS; HTTP is allowed only on loopback.")
    path = parts.path.rstrip("/")
    if inference and not path.endswith("/v1"):
        path += "/v1"
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _prompt_url(label, default, *, inference=False, supplied=None):
    if supplied is not None:
        return validate_url(supplied, inference=inference)
    try:
        default = validate_url(default, inference=inference)
    except SetupError:
        default = DEFAULT_BASE_URL if inference else DEFAULT_MCP_URL
    while True:
        value = input(f"{label} [{default}]: ").strip() or default
        try:
            return validate_url(value, inference=inference)
        except SetupError as exc:
            print(str(exc))


def _confirm(prompt):
    return input(f"{prompt} [y/N]: ").strip().lower() in {"y", "yes"}


def _safe_model_id(value):
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= 512
        and value == value.strip()
        and value.isprintable()
    )


def parse_catalog(payload):
    """Return unique model IDs and only context limits explicitly advertised."""
    entries = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise SetupError("The endpoint returned an unsupported model catalog.")
    models = []
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict) or not _safe_model_id(entry.get("id")):
            continue
        model_id = entry["id"]
        if model_id in seen:
            continue
        seen.add(model_id)
        model = {"id": model_id}
        context = entry.get("context_length")
        if isinstance(context, int) and not isinstance(context, bool) and context > 0:
            model["context_length"] = context
        models.append(model)
    return models


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward an inference credential to a redirected destination.
        raise SetupError("The model catalog redirected. Enter its final base URL and try again.")


def fetch_models(base_url, api_key):
    """Use the free catalog endpoint only; no generation or test inference call."""
    base_url = validate_url(base_url, inference=True)
    # validate_url limits schemes to HTTPS or loopback HTTP; redirects are blocked below.
    request = urllib.request.Request(  # noqa: S310
        base_url + "/models",
        headers={"Authorization": "Bearer " + api_key, "Accept": "application/json"},
        method="GET",
    )
    try:
        opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(request, timeout=20) as response:
            data = response.read(MAX_CATALOG_BYTES + 1)
        if len(data) > MAX_CATALOG_BYTES:
            raise SetupError("The model catalog is too large. Verify the inference endpoint.")
        return parse_catalog(json.loads(data))
    except SetupError:
        raise
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            raise SetupError(
                "The catalog rejected access. Verify the project API key and permissions."
            ) from None
        raise SetupError(
            "The model catalog is unavailable. Verify the inference endpoint and try again."
        ) from None
    except Exception:
        # Response bodies, network exceptions and request objects may contain secrets.
        raise SetupError(
            "Could not read the model catalog. Check the endpoint and connection, then retry."
        ) from None


def _choose_model(models, current, api_key):
    models = [
        model for model in models if _safe_model_id(model.get("id")) and api_key not in model["id"]
    ]
    print("Available Aixy models:")
    for index, model in enumerate(models, 1):
        context = model.get("context_length")
        suffix = (
            f" ({context} context tokens)"
            if isinstance(context, int) and not isinstance(context, bool) and context > 0
            else " (context not advertised)"
        )
        print(f"  {index}. {model['id']}{suffix}")
    if not models:
        print(
            "  No model IDs advertised. Enter an exact model or route ID "
            "and its verified context limit."
        )
    known = {model["id"]: model for model in models}
    default = current if current in known else models[0]["id"] if models else ""
    while True:
        suffix = f" [{default}]" if default else ""
        selected = input(f"Model ID or list number{suffix}: ").strip() or default
        if selected.isdigit() and 1 <= int(selected) <= len(models):
            selected = models[int(selected) - 1]["id"]
        if _safe_model_id(selected) and api_key not in selected:
            break
        print("Enter a valid model ID or list number.")
    context = known.get(selected, {}).get("context_length")
    if not isinstance(context, int) or isinstance(context, bool) or context <= 0:
        while True:
            text = input(
                "Verified context length in tokens (required; consult the model or route owner): "
            ).strip()
            try:
                context = int(text.replace(",", "").replace("_", ""))
            except ValueError:
                context = 0
            if context > 0:
                break
            print("Enter the verified positive integer context limit; no default is assumed.")
    return selected, context


def _existing_aixy(config):
    existing = config.get("providers", {}).get("aixy")
    legacy = [
        entry
        for entry in config.get("custom_providers", [])
        if isinstance(entry, dict) and str(entry.get("name", "")).lower() == "aixy"
    ]
    return existing, legacy


def _inference_config(raw, base_url, model_id, context):
    result = copy.deepcopy(raw)
    # Replacement is confirmed before prompting for a key. Do not carry a
    # previous endpoint's credential commands, headers or disabled flag forward.
    result.setdefault("providers", {})["aixy"] = {
        "api": base_url,
        "api_key": API_KEY_REF,
        "transport": "openai_chat",
    }
    if "custom_providers" in result:
        result["custom_providers"] = [
            entry
            for entry in result["custom_providers"]
            if not (isinstance(entry, dict) and str(entry.get("name", "")).lower() == "aixy")
        ]
    model = result.get("model")
    model = (
        {key: value for key, value in model.items() if key not in _MODEL_ROUTE_KEYS}
        if isinstance(model, dict)
        else {}
    )
    model.update(
        {
            "provider": "aixy",
            "default": model_id,
            "context_length": context,
            "base_url": "",
            "api_key": "",
            "key_env": "",
            "api_key_env": "",
            "api_mode": "chat_completions",
            "openai_runtime": "auto",
        }
    )
    result["model"] = model
    return result


def run_setup(args):
    """Configure inference and offer optional MCP only after inference is saved."""
    try:
        _require_interactive_terminal()
        config, raw = _writable_config()
        existing, legacy = _existing_aixy(raw)
        if (existing is not None or legacy) and not _confirm(
            "An Aixy provider already exists. Replace its connection settings?"
        ):
            print("Existing Aixy inference settings kept.")
            return 0
        existing_url = (
            existing.get("api", DEFAULT_BASE_URL)
            if isinstance(existing, dict)
            else DEFAULT_BASE_URL
        )
        base_url = _prompt_url(
            "Aixy inference base URL",
            existing_url,
            inference=True,
            supplied=getattr(args, "base_url", None),
        )
        # A credential saved for a different deployment must never be sent to
        # a newly entered gateway just because the user accepted a blank prompt.
        try:
            same_endpoint = (
                existing is not None and validate_url(existing_url, inference=True) == base_url
            )
        except SetupError:
            same_endpoint = False
        saved_key = (_get_secret(API_KEY_ENV) or "") if same_endpoint else ""
        prompt = (
            "Aixy project API key (leave blank to keep saved key): "
            if saved_key
            else "Aixy project API key: "
        )
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            entered_key = getpass.getpass(prompt).strip()
        api_key = entered_key or saved_key
        if not api_key or any(ord(char) <= 32 or ord(char) >= 127 for char in api_key):
            raise SetupError("Enter a valid project API key using a private interactive terminal.")
        models = fetch_models(base_url, api_key)
        current = raw.get("model", {})
        current_id = (
            current.get("default", "")
            if isinstance(current, dict) and current.get("provider") == "aixy"
            else ""
        )
        model_id, context = _choose_model(models, current_id, api_key)
        print(f"Aixy inference: {base_url}; model {model_id}; context {context} tokens.")
        print("This makes the selected Aixy model the active default for new Hermes sessions.")
        if not _confirm("Save inference settings and use this active model?"):
            print("Cancelled. Inference settings and credentials were not changed.")
            return 0
        # Recheck before any credential write and merge into the latest unrelated settings.
        config, fresh = _writable_config()
        if any(
            fresh.get(section) != raw.get(section)
            for section in ("providers", "custom_providers", "model")
        ):
            raise SetupError(
                "Hermes inference settings changed during setup. Run hermes aixy setup again."
            )
        proposed = _inference_config(fresh, base_url, model_id, context)
        try:
            if entered_key:
                receipt = config.save_env_value_secure(API_KEY_ENV, api_key)
                if not isinstance(receipt, dict) or receipt.get("success") is not True:
                    raise SetupError("Hermes did not save the project API key.")
            # This is the complete fresh raw mapping. Deep-merging it would
            # resurrect deliberately removed credential and provider fields.
            config.save_config(proposed, merge_existing=False)
        except Exception:
            raise SetupError(
                "Could not save Aixy inference settings. Run hermes aixy setup again."
            ) from None
        print(
            "Aixy inference configured. The project key is stored by Hermes; "
            "no inference request was sent."
        )
        print("Start a new Hermes session to use this default model.")
        if _confirm("Connect Aixy tools with MCP?"):
            return run_mcp(argparse.Namespace(url=getattr(args, "mcp_url", None)))
        print("MCP setup skipped. You can connect it later with: hermes aixy mcp")
        return 0
    except (EOFError, KeyboardInterrupt):
        print("\nSetup cancelled. Previously saved settings are kept.")
        return 0
    except getpass.GetPassWarning:
        print("A private interactive terminal is required to enter the project API key.")
        return 1
    except SetupError as exc:
        print(str(exc))
        return 1
    except Exception:
        print(
            "Aixy setup could not finish. Check your Hermes installation "
            "and retry hermes aixy setup."
        )
        return 1


def _native_mcp_add(url):
    """Delegate authorization, credential storage and tool selection to Hermes."""
    _require_interactive_terminal()
    hermes_config = _config_module()
    # A profile can be selected through a context-local home rather than the
    # process environment. Pin it explicitly for the child CLI's token storage.
    child_env = dict(os.environ)
    child_env["HERMES_HOME"] = str(hermes_config.get_hermes_home())
    # The native CLI must prompt normally, never inherit a caller's unattended
    # or YOLO launch policy. Explicit false values also prevent profile defaults
    # from reintroducing those flags while the child starts up.
    for name in (*_UNATTENDED_ENV, "HERMES_YOLO_MODE"):
        child_env[name] = "0"
    # Fixed interpreter/module, separate validated URL argument, no shell or secret arguments.
    completed = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "hermes_cli.main",
            "mcp",
            "add",
            "aixy",
            "--url",
            url,
            "--auth",
            "oauth",
        ],
        check=False,
        env=child_env,
        cwd=Path(hermes_config.__file__).resolve().parent.parent,
    )
    if completed.returncode:
        raise SetupError("Hermes MCP setup did not finish.")


def run_mcp(args):
    """MCP can be configured independently without reading an inference secret."""
    try:
        _require_interactive_terminal()
        _, raw = _writable_config()
        existing = raw.get("mcp_servers", {}).get("aixy")
        default = (
            existing.get("url", DEFAULT_MCP_URL) if isinstance(existing, dict) else DEFAULT_MCP_URL
        )
        url = _prompt_url("Aixy MCP endpoint URL", default, supplied=getattr(args, "url", None))
        print("Hermes will open browser OAuth and let you choose the exposed tools.")
        print("Existing MCP entries are kept unless you approve Hermes' replacement prompt.")
        _native_mcp_add(url)
        _, updated = _writable_config()
        saved = updated.get("mcp_servers", {}).get("aixy")
        if saved == existing and existing is not None:
            print("Existing Aixy MCP settings kept. To reconnect later: hermes aixy mcp")
            return 0
        if (
            isinstance(saved, dict)
            and saved.get("url") == url
            and saved.get("auth") == "oauth"
            and saved.get("enabled", True) is not False
        ):
            print("Aixy MCP configured. Start a new session or use /reload-mcp.")
            return 0
        print("Aixy MCP setup did not finish. Retry with: hermes aixy mcp")
        print("Previously saved inference settings are kept.")
        return 1
    except (EOFError, KeyboardInterrupt):
        print("\nMCP setup cancelled. Previously saved inference settings are kept.")
        return 0
    except SetupError as exc:
        print(str(exc))
        print("Retry with: hermes aixy mcp. Previously saved inference settings are kept.")
        return 1
    except Exception:
        print("Aixy MCP setup did not finish. Retry with: hermes aixy mcp")
        print("Previously saved inference settings are kept.")
        return 1


def handle_command(args):
    if getattr(args, "aixy_command", None) == "setup":
        return run_setup(args)
    if getattr(args, "aixy_command", None) == "mcp":
        return run_mcp(args)
    print("Usage: hermes aixy setup [--base-url URL] [--mcp-url URL] | hermes aixy mcp [--url URL]")
    return 0
