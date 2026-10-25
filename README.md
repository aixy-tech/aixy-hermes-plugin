# Aixy for Hermes

Configure [Hermes Agent](https://github.com/NousResearch/hermes-agent) to use an
[Aixy](https://aixy-gateway.com) project for inference, with an optional connection to your Aixy
tools through MCP. MCP is **off by default** and can be connected later.

## Install

Requires Hermes 0.22.5 or later and Python 3.11 or later. The plugin has no additional Python
dependencies. Run these commands in an interactive terminal on the machine running Hermes:

```bash
hermes plugins install aixy-tech/aixy-hermes-plugin --enable
hermes aixy setup
```

Use the intended Hermes profile (`HERMES_HOME`) for both commands. Installing or enabling the
plugin registers its CLI commands; it does not contact Aixy or change inference or MCP settings.
This repository is maintained by Aixy. A Hermes catalog submission is separate from direct
installation and does not imply review or approval by Nous Research.

## Configure inference

The setup command asks for your Aixy inference base URL, defaulting to
`https://api.aixy-gateway.com/v1`, and a project-scoped Aixy API key through hidden input. It lists
the project's available model IDs using that endpoint's `/models` catalog, without running model
inference. Choose an exact model or route ID. When the catalog does not provide a usable context
limit, enter a limit verified for that model or route. Setup does not guess one.

Before saving, setup shows the selected provider and model and asks for confirmation. It also
asks before replacing an existing Aixy connection. Other providers, MCP connections, and unrelated
profile settings are preserved. The selected `aixy` provider uses Hermes' native OpenAI Chat
Completions transport and becomes the main chat model for new sessions. Review auxiliary-model
overrides separately.

Hermes stores the project key through its credential API as `AIXY_API_KEY`; the provider settings
contain a variable reference. Keep keys out of chat, shell arguments, shared shell profiles, and
repositories. A saved key can be reused only for its existing gateway; changing gateways requires
entering a key again.

The final prompt offers optional MCP setup with **[y/N]**. Press Enter to finish with inference
only. Skipping leaves all MCP connections untouched, including an existing Aixy connection.

## Connect MCP now or later

Choose **yes** at the end of setup, or run this independent command whenever you are ready:

```bash
hermes aixy mcp
```

Confirm the MCP endpoint, complete Hermes' native OAuth sign-in in the browser, review your Aixy
organization and optional project restriction, and select the tools to enable. This command does
not ask for an inference API key. Inference and MCP use separate credentials and scopes. A failed
or cancelled MCP sign-in leaves saved inference settings intact. An existing `aixy` MCP connection
is preserved unless you explicitly choose to replace it.

The default MCP endpoint is `https://api.aixy-gateway.com/mcp`. It is independently editable for
custom deployments. Both endpoint URLs support path prefixes. HTTPS is required except for
loopback development endpoints. Start a new Hermes session or use `/reload-mcp` after connecting
tools. The plugin preserves Hermes' normal tool approval policy.

For a custom deployment, you can supply endpoint URLs without putting credentials on the command
line:

```bash
hermes aixy setup --base-url https://gateway.example.com/aixy/v1
hermes aixy mcp --url https://gateway.example.com/aixy/mcp
```

Send a short prompt yourself and verify the request in Aixy Activity. Saved settings and catalog
discovery do not establish that model inference works. Check your available MCP tools separately.

## Permissions and effects

- **Network:** explicit inference setup sends the project key in the authorization header of an
  HTTPS `GET /models` request to your chosen gateway. Catalog redirects are rejected. The plugin
  sends no prompts or inference requests during setup. Optional MCP setup contacts your chosen
  MCP endpoint and the authorization service discovered by Hermes for browser OAuth.
- **Profile files and credentials:** the commands read the selected Hermes profile. Confirmed
  inference setup updates its provider and model settings and stores the project key using Hermes'
  credential writer. MCP authorization can write MCP settings and OAuth credentials through
  Hermes. Never run setup against someone else's profile.
- **Processes:** MCP setup launches the current Python interpreter with
  `-m hermes_cli.main mcp add aixy --url <endpoint> --auth oauth` in the installed Hermes directory.
  It uses the selected `HERMES_HOME` and copies the Hermes process environment, with
  `HERMES_NONINTERACTIVE`, `HERMES_SINGLE_QUERY_SESSION`, `HERMES_CRON_SESSION`, and
  `HERMES_YOLO_MODE` set to `0` in the child. Setup requires an interactive terminal and rejects
  unattended sessions. The caller's environment is unchanged. Keys are not passed in command
  arguments. Hermes owns browser authorization, OAuth storage, and tool selection.
- **Activation:** importing, installing, or enabling the plugin does not run these setup actions.
  The plugin exposes CLI commands only, with no automatic hooks or background services.
  It adds no telemetry or self-updating code.

## Development and validation

Compatibility was checked against Hermes 0.22.5, commit
`bf0203a794a07aac3ff764972ab9087baa4e4f4e`. Run local checks from the repository root:

```bash
python3 -m unittest discover -s tests -v
uvx ruff@0.17.2 check .
uvx ruff@0.17.2 format --check .
hermes plugins doctor . --ci
hermes plugins validate .
```

Use a disposable `HERMES_HOME` for development checks. Unit tests use fake keys, mocked catalog
responses, and mocked OAuth. They do not read your Hermes profile, contact real gateways, or make
paid model calls. To test installation from committed local source:

```bash
hermes plugins install "file://$(pwd)" --enable
```

Hermes clones committed source, so uncommitted working-tree changes are not installed. The native
installation and doctor checks require an installed Hermes runtime; the offline tests require
only the Python standard library.

See Hermes' [plugin guide](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins),
[model configuration](https://hermes-agent.nousresearch.com/docs/user-guide/configuring-models),
and [MCP guide](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp) for upstream
behavior.

## License

The plugin is [MIT licensed](LICENSE). Aixy names and logos remain the property of their owner.
