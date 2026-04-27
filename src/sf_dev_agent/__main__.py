"""CLI entry point for the Salesforce Developer Agent.

After `uv pip install -e .`, the binaries `sf-agent` and `sfagent` are
installed on PATH. Both invoke this entry point.

Usage:
    sf-agent setup                          # interactive wizard
    sf-agent doctor                         # system prereq check
    sf-agent                                # interactive REPL
    sf-agent "Create an Account trigger"    # one-shot task
    sf-agent --provider gemini "..."        # provider override
    sf-agent resume <task-id>               # resume a persisted task
    sf-agent memory <verb>                  # extract / export / promote
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel

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

    # Special-case the memory subcommand — uses its own argparse + dispatch.
    if len(sys.argv) >= 2 and sys.argv[1] == "memory":
        from sf_dev_agent.memory_cli import run_memory_command
        sys.exit(run_memory_command(sys.argv[2:]))

    # Special-case the doctor subcommand — system prereq check.
    if len(sys.argv) >= 2 and sys.argv[1] == "doctor":
        from sf_dev_agent.doctor import main as doctor_main
        sys.exit(doctor_main(sys.argv[2:]))

    # Special-case the resume subcommand — pick up a persisted task.
    if len(sys.argv) >= 2 and sys.argv[1] == "resume":
        from sf_dev_agent.resume_cli import run_resume_command
        sys.exit(run_resume_command(sys.argv[2:]))

    args = parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if not args.org_alias:
        console.print(
            "[bold red]No Salesforce org configured.[/bold red] "
            "Run [cyan]sf-agent setup[/cyan] to get started, "
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

    # Layer A: soft-prompt to warm the context engine on first run for
    # this org. Skipped automatically in mock-org mode and once the user
    # has chosen "no-and-stop-asking" for this org.
    from sf_dev_agent.warmup import prompt_warmup_if_needed
    prompt_warmup_if_needed(org=org, mock_org=args.mock_org)

    if args.request:
        # One-shot path — single AgentLoop, exit when the task ends.
        agent = AgentLoop(org=org, provider=provider, mock_org=args.mock_org)
        mock_label = " [bold yellow][MOCK ORG][/bold yellow]" if args.mock_org else ""
        console.print(
            Panel(
                f"[bold]Salesforce Developer Agent[/bold]{mock_label}\n"
                f"Org: {org.org_alias} ({org.org_type}) | API: v{org.api_version}\n"
                f"Provider: {provider.__class__.__name__} | "
                f"Model: {provider.model_name}",
                border_style="green",
            )
        )
        agent.run(args.request)
        return

    # Interactive REPL path — `prompt_toolkit`-based persistent session.
    # Falls back to the simpler one-shot loop if stdin isn't a TTY (e.g.
    # piped input in CI). See repl.py for the slash-command surface.
    if not sys.stdin.isatty():
        console.print(
            "[bold red]No request given and stdin is not a TTY.[/bold red] "
            "Pass a request as an argument, or run [cyan]sf-agent[/cyan] "
            "from an interactive terminal."
        )
        sys.exit(1)

    from sf_dev_agent.repl import launch_repl
    sys.exit(launch_repl(
        org=org, provider=provider, mock_org=args.mock_org,
    ))


if __name__ == "__main__":
    main()
