# Manual inference smoke

This development runner requires **separate explicit authorization** to spend up to USD 1. It does
not create a project, change a policy, obtain a credential, or connect OAuth/MCP. Use a disposable
project whose shared lifetime **hard budget is USD 1**, with prompt/response capture disabled and
only the approved model route available. Use the prepared Hermes 0.22.5 baseline in [README](README.md).

Before execution, verify the project's effective model prices, context window and cache threshold.
Save non-secret JSON settings outside the disposable source checkout:

```json
{
  "base_url": "https://api.aixy-gateway.com/v1",
  "model": "<exact approved project model ID>",
  "context_length": 65536,
  "max_input_usd_per_million": "<verified maximum input tariff>",
  "max_output_usd_per_million": "<verified maximum output tariff>",
  "cache_min_tokens": 2048,
  "output_limit": 256,
  "reasoning_effort": "low"
}
```

Replace all placeholders and the context/cache limits with verified values. The input tariff must
be the maximum across ordinary input and **every applicable cache-write price/TTL**; the output
tariff must cover reasoning and visible output. The runner supports token-only tariffs with no
per-request fee or other surcharge. Do not use it for a route whose complete maximum price is
unknown. These upper bounds complement the server's hard budget; the report is not an invoice or
proof of settled billing.

Validate the plan **offline**, without a key or any HTTP request:

```bash
PYTHONDONTWRITEBYTECODE=1 AIXY_HERMES_SOURCE=/absolute/path/to/hermes-agent \
  /absolute/path/to/prepared-hermes-python tests/native/live_smoke.py \
  --config /private/path/settings.json --preflight-only
```

After authorization, run in a private interactive terminal. The project key is requested through
hidden input; never put it in the settings, command line, environment, chat, or report:

```bash
PYTHONDONTWRITEBYTECODE=1 AIXY_HERMES_SOURCE=/absolute/path/to/hermes-agent \
  /absolute/path/to/prepared-hermes-python tests/native/live_smoke.py \
  --config /private/path/settings.json --execute --key-stdin \
  --report /private/path/new-smoke-report.json
```

The report path must be new. `--key-stdin` uses `getpass` on a TTY and reads one line on a private
pipe. A missing private TTY fails instead of falling back to echoed key entry. The key lives only
in memory and the disposable Hermes profile; that profile and all main/auxiliary clients close on
exit. The runner preserves `HOME`, clears inherited credentials/proxies, and suppresses raw logs,
payloads, model responses and exception messages. Reports contain only synthetic correlation IDs,
HTTP status, token/cache counts, request size and conservative token-price bounds.

There are seven sequential phases, with one authorized HTTP send each: a forced local arithmetic
tool call, its result, native Hermes SSE assembly/callback delivery, an actual auxiliary call,
a second conversation, and two requests with an identical inert hash prefix. The runner uses
`AIAgent` request methods rather than the autonomous conversation loop, avoiding auto-title,
compaction and iteration-summary requests. The auxiliary request uses an explicit body override
for the output cap because Hermes omits `max_tokens` on generic custom auxiliary routes.

A transport gate rejects redirects, extra endpoints/models/sessions, hidden retries, missing or
increased output caps, and sends without a phase permit **before network access**. Failed sends
still count. The global ceiling is eight sends; the nominal plan uses seven. Each ASCII JSON body
is at most 8,192 bytes, a deliberately stricter conservative bound than 8,192 input tokens, and
each output is at most the configured 256-token ceiling. Socket/DNS access is allowed only while
the permitted synchronous transport sends; asynchronous transport is rejected. The fake responder
used by self-tests never gets socket permission.

Native context metadata is supplied from the verified settings. Environment probes, background
review, memory, compression, project plugins, telemetry, SDK retries and ordinary app recovery are
disabled. Hermes' remaining compatibility/auxiliary retries are stopped by the one-send gate.
Reasoning effort `low` must be present on every outgoing request. The runner stops on the first
error or unknown/excess usage. A missing implicit cache hit or an input below the configured cache
threshold yields **inconclusive**, with no extra warm-up/retry; no provider-specific cache controls
are injected. Review Activity and Sessions separately and retire the disposable project/key after
the accounting check. Run the native unittest command from [README](README.md) for fake-only tests.
