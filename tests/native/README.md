# Native Hermes contract checks

These opt-in tests use a prepared Hermes checkout and its Python environment. They do not install
or update Hermes. The verified baseline is Hermes **0.22.5**, commit
`bf0203a794a07aac3ff764972ab9087baa4e4f4e`.

From this plugin's root, select that source checkout and run its prepared Python interpreter:

```bash
PYTHONDONTWRITEBYTECODE=1 AIXY_HERMES_SOURCE=/absolute/path/to/hermes-agent \
  /absolute/path/to/prepared-hermes-python -m unittest discover -s tests/native -v
```

The ordinary `python3 -m unittest discover -s tests -v` suite remains independent of Hermes.
Without `AIXY_HERMES_SOURCE`, native-client cases skip and the dependency-free request-gate cases run.

The native suite creates and removes a disposable `HERMES_HOME`, keeps `HOME` unchanged, strips
credential environment variables, uses fake keys, and blocks DNS and outbound sockets before
importing Hermes. Its test configuration disables Hermes' background environment probe.
HTTP responses are simulated. It checks saved configuration and
runtime resolution, main Chat/Responses builders, tool-call normalization, SDK SSE decoding and
cache-stat extraction, auxiliary routing, conversation scope through compaction/reset, and context
metadata/override/fallback behavior. An opaque route without metadata currently falls back to
256,000 tokens in this Hermes version; use a verified explicit context override when needed.

These checks do not execute a complete agent turn, prove real cache hits or provider inference,
or authorize MCP OAuth. Keep live-account verification separate. Internal Hermes test seams are
version-sensitive; failures after changing Hermes require review, not automatic baseline changes.

The [manual inference smoke runner](LIVE_SMOKE.md) is separate from routine validation. Its automated
self-tests use only fake keys and fake HTTP; live execution requires explicit authorization.
