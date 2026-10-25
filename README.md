# Aixy for Hermes

Connect [Hermes Agent](https://github.com/NousResearch/hermes-agent) to
[Aixy](https://aixy-gateway.com) for inference and optional MCP tools.

Requires Hermes 0.22.5+ and an interactive terminal.

## Setup

```bash
hermes plugins install aixy-tech/aixy-hermes-plugin --enable
hermes aixy setup
```

Enter your gateway, project API key (hidden), and model. If prompted, enter the model's verified
context limit. Confirm to make Aixy your default provider, then start a new Hermes session.

## Optional MCP

Setup offers to connect MCP through browser OAuth. The default is **No**. To connect it later:

```bash
hermes aixy mcp
```

Choose your tools, then start a new session or run `/reload-mcp`.

## What setup changes

Setup reads and updates the selected Hermes profile. It fetches your gateway's model catalog using
the project key and stores that key through Hermes. It sends no inference requests.

MCP setup launches Hermes' native CLI with the selected profile and process environment, with
unattended and YOLO flags disabled. It contacts your MCP endpoint and OAuth service and saves the
connection settings and credentials through Hermes. Normal tool approvals remain in place.

The plugin adds no telemetry or background services.

[MIT license](LICENSE).
