# Plugin maintenance

This repository distributes the standalone Aixy integration for Hermes Agent. Keep gateway
implementation, internal documentation, credentials, and tenant data out of it.

- Keep source, tests, documentation, and commit metadata in English.
- Use Hermes' public plugin registration, provider configuration, credential helpers, and native
  MCP OAuth flow. Do not fork or patch Hermes.
- Loading and registering the plugin must have no profile, credential, network, or process effects.
  Run setup only after an explicit CLI command in a private interactive terminal.
- Keep inference setup separate from personal MCP OAuth. MCP is an explicit default-off choice
  and must remain available later through `hermes aixy mcp`.
- Preserve unrelated profile settings and connections. Require confirmation before replacing Aixy
  settings. Refuse unreadable or managed configuration before writing credentials.
- Keep gateway and credential ownership aligned. Never reuse an existing inference key for a
  different gateway. Never print secrets, server response bodies, or secret-bearing exceptions.
- Preserve Hermes' normal action approval policy. Do not add tool preapprovals or honor environment
  bypasses when launching native setup. Describe network, file, process, and credential effects
  accurately in the README.
- Use only fake keys and mocked gateways in tests. Do not read personal Hermes configuration,
  initiate real OAuth, or make paid model calls during validation. Run native checks with a
  disposable `HERMES_HOME`.
- Before submitting changes, run `python3 -m unittest discover -s tests -v`,
  `uvx ruff@0.17.2 check .`, `uvx ruff@0.17.2 format --check .`, and `git diff --check`.
  For plugin contract or packaging changes, also run `hermes plugins doctor . --ci` and
  `hermes plugins validate .` with a disposable profile.
- Use a focused issue, dedicated branch/worktree, and linked pull request for maintainer-requested
  changes. Keep local validation, automatic CI, direct installation, Hermes catalog review,
  publication, and real-account verification distinct.
- Do not manually dispatch, rerun, or enable GitHub Actions without a maintainer's specific request.
- Increment `plugin.yaml` and `pyproject.toml` versions together for releases. Do not add or change
  a license without the maintainer's decision.
