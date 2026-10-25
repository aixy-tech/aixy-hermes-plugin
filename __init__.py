"""Aixy setup commands for Hermes Agent.

Importing or registering this plugin never reads credentials, changes configuration,
or connects to a service. All configuration is requested by an explicit CLI command.
"""

from .aixy_setup import handle_command, setup_parser


def register(ctx):
    """Expose the interactive setup under ``hermes aixy``."""
    ctx.register_cli_command(
        name="aixy",
        help="Connect Hermes to Aixy inference and optional MCP tools",
        setup_fn=setup_parser,
        handler_fn=handle_command,
    )
