"""CLI entry point for the Salesforce Developer Agent.

Usage:
    uv run python -m sf_dev_agent setup                          # interactive wizard
    uv run python -m sf_dev_agent                                # interactive REPL
    uv run python -m sf_dev_agent "Create an Account trigger"    # one-shot
    uv run python -m sf_dev_agent --provider gemini "..."        # provider override
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt

from sf_dev_agent.agent import AgentLoop
from sf_dev_agent.models.schemas import OrgConnection
from sf_dev_agent.providers import PROVIDERS, create_provider
from sf_dev_agent.sf_config import (
    derive_api_version,
    derive_instance_url,
    derive_org_type,
)

console = Console()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Salesforce Developer Agent — AI-powered Salesforce development",
    )
    parser.add_argument(
        "request",
        nargs="?",
        default=None,
        help=(
            "The task to perform, OR the literal word 'setup' to launch the "
            "interactive setup wizard. Omit for interactive REPL mode."
        ),
    )
    parser.add_argument(
        "--provider",
        choices=list(PROVIDERS),
        default=None,
        help="LLM provider (default: LLM_PROVIDER env, or auto-detected from set API key)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Model name override (default: each provider's built-in default)",
    )
    parser.add_argument(
        "--org-alias",
        default=os.environ.get("SF_ORG_ALIAS"),
        help="Salesforce org alias (default: SF_ORG_ALIAS env var)",
    )
    parser.add_argument(
        "--org-type",
        choices=["sandbox", "scratch", "production", "developer"],
        default=None,
        help="Override auto-detected org type",
    )
    parser.add_argument(
        "--instance-url",
        default=None,
        help="Override auto-detected instance URL",
    )
    parser.add_argument(
        "--api-version",
        default=None,
        help="Override API version (default: read from workspace/sfdx-project.json)",
    )
    parser.add_argument(
        "--mock-org",
        action="store_true",
        help=(
            "Stub all Salesforce CLI tool calls with canned responses. "
            "Lets you test the full agent loop without a live org or sf CLI."
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    return parser.parse_args(argv)


def main() -> None:
    load_dotenv()

    # Special-case the setup wizard before normal arg parsing — it has no other flags.
    if len(sys.argv) >= 2 and sys.argv[1] == "setup":
        from sf_dev_agent.setup_wizard import run_setup
        run_setup()
        return

    args = parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if not args.org_alias:
        console.print(
            "[bold red]No Salesforce org configured.[/bold red] "
            "Run [cyan]uv run python -m sf_dev_agent setup[/cyan] to get started, "
            "or set SF_ORG_ALIAS in your .env file."
        )
        sys.exit(1)

    # Org type / instance URL are auto-detected via sf CLI; CLI flags or env vars override.
    org_type = args.org_type or os.environ.get("SF_ORG_TYPE") or derive_org_type(args.org_alias)
    instance_url = (
        args.instance_url
        or os.environ.get("SF_INSTANCE_URL")
        or derive_instance_url(args.org_alias)
    )
    api_version = (
        args.api_version
        or os.environ.get("SF_API_VERSION")
        or derive_api_version()
    )

    org = OrgConnection(
        tenant_id="local-dev",
        org_alias=args.org_alias,
        org_type=org_type,
        instance_url=instance_url,
        api_version=api_version,
    )

    try:
        provider = create_provider(provider=args.provider, model=args.model)
    except ImportError as exc:
        console.print(f"[bold red]Provider not installed:[/bold red] {exc}")
        sys.exit(1)
    except ValueError as exc:
        console.print(f"[bold red]{exc}[/bold red]")
        sys.exit(1)

    agent = AgentLoop(org=org, provider=provider, mock_org=args.mock_org)

    mock_label = " [bold yellow][MOCK ORG][/bold yellow]" if args.mock_org else ""
    console.print(
        Panel(
            f"[bold]Salesforce Developer Agent[/bold]{mock_label}\n"
            f"Org: {org.org_alias} ({org.org_type}) | API: v{org.api_version}\n"
            f"Provider: {provider.__class__.__name__} | Model: {provider.model_name}\n\n"
            "Type your request, or 'quit' to exit.",
            border_style="green",
        )
    )

    if args.request:
        agent.run(args.request)
    else:
        while True:
            try:
                user_input = Prompt.ask("\n[bold green]sf-agent[/bold green]")
            except (KeyboardInterrupt, EOFError):
                console.print("\n[dim]Goodbye.[/dim]")
                break

            if user_input.strip().lower() in ("quit", "exit", "q"):
                console.print("[dim]Goodbye.[/dim]")
                break

            if not user_input.strip():
                continue

            agent.run(user_input.strip())


if __name__ == "__main__":
    main()
