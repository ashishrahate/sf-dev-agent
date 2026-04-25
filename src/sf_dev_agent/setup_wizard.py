"""Interactive setup wizard.

Walks a fresh user through:
  1. Verifying sf CLI is installed
  2. Picking (or logging into) a Salesforce org
  3. Picking an LLM provider and supplying a key
  4. Validating the key with a one-token API call
  5. Writing a minimal .env

Designed so that 90% of users only have to provide an API key and pick an org
from a list. Everything else is auto-derived at runtime.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from sf_dev_agent.paths import repo_root
from sf_dev_agent.providers import PROVIDER_KEY_VARS

console = Console()

PROVIDER_INFO = {
    "gemini": {
        "label": "Google Gemini",
        "key_env": "GOOGLE_API_KEY",
        "key_url": "https://aistudio.google.com/apikey",
        "free_tier": "Free — up to 250 req/day for gemini-2.5-flash",
    },
    "openai": {
        "label": "OpenAI",
        "key_env": "OPENAI_API_KEY",
        "key_url": "https://platform.openai.com/api-keys",
        "free_tier": "Pay-as-you-go (no free tier)",
    },
    "anthropic": {
        "label": "Anthropic Claude",
        "key_env": "ANTHROPIC_API_KEY",
        "key_url": "https://console.anthropic.com/settings/keys",
        "free_tier": "Pay-as-you-go (no free tier)",
    },
}


def _sf_exe() -> str:
    return "sf.cmd" if sys.platform == "win32" else "sf"


# ----------------------------------------------------------------------
# Step 1 — preflight
# ----------------------------------------------------------------------

def check_sf_cli() -> bool:
    """Confirm `sf` CLI is on PATH and runnable."""
    if not shutil.which(_sf_exe()):
        console.print(
            "[bold red]sf CLI not found.[/bold red] "
            "Install it with: [cyan]npm install -g @salesforce/cli[/cyan] "
            "(requires Node 18+)."
        )
        return False
    try:
        proc = subprocess.run(
            [_sf_exe(), "--version"], capture_output=True, text=True, timeout=10,
        )
        console.print(f"[green]✓[/green] sf CLI: [dim]{proc.stdout.strip()}[/dim]")
        return True
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        console.print(f"[bold red]sf CLI failed:[/bold red] {exc}")
        return False


# ----------------------------------------------------------------------
# Step 2 — org selection
# ----------------------------------------------------------------------

def list_connected_orgs() -> list[dict]:
    """Return all non-expired connected orgs from `sf org list --json`."""
    try:
        proc = subprocess.run(
            [_sf_exe(), "org", "list", "--json"],
            capture_output=True, text=True, timeout=20,
        )
        data = json.loads(proc.stdout) if proc.stdout else {}
        result = data.get("result", {}) if data.get("status") == 0 else {}
        orgs: list[dict] = []
        for bucket in ("nonScratchOrgs", "scratchOrgs", "devHubs", "sandboxes"):
            for o in result.get(bucket, []) or []:
                if o.get("connectedStatus") == "Connected":
                    orgs.append(o)
        # Dedupe by username, preserve order.
        seen, unique = set(), []
        for o in orgs:
            u = o.get("username")
            if u and u not in seen:
                seen.add(u)
                unique.append(o)
        return unique
    except (subprocess.SubprocessError, json.JSONDecodeError):
        return []


def login_new_org(alias: str) -> bool:
    """Run `sf org login web --alias <alias>` interactively (opens browser)."""
    console.print(
        f"\nA browser will open. Log in to your Salesforce org — "
        f"the agent will save it as alias [cyan]{alias}[/cyan]."
    )
    if not Confirm.ask("Continue?", default=True):
        return False
    try:
        result = subprocess.run(
            [_sf_exe(), "org", "login", "web", "--alias", alias],
            timeout=300,
        )
        return result.returncode == 0
    except subprocess.SubprocessError as exc:
        console.print(f"[bold red]Login failed:[/bold red] {exc}")
        return False


def pick_org() -> str | None:
    """Show connected orgs as a numbered menu; return chosen alias."""
    orgs = list_connected_orgs()

    if orgs:
        table = Table(title="Connected Salesforce orgs")
        table.add_column("#", style="cyan", justify="right")
        table.add_column("Alias", style="bold")
        table.add_column("Username")
        table.add_column("Type", style="dim")
        for i, o in enumerate(orgs, 1):
            kind = (
                "Scratch" if o.get("isScratch")
                else "Sandbox" if o.get("isSandbox")
                else "Dev Hub" if o.get("isDevHub")
                else "Developer/Prod"
            )
            table.add_row(
                str(i),
                o.get("alias") or "[no alias]",
                o.get("username") or "",
                kind,
            )
        console.print(table)
        console.print("[dim]N) Log in to a new org[/dim]")

        choice = Prompt.ask(
            "\nPick an org",
            choices=[str(i) for i in range(1, len(orgs) + 1)] + ["N", "n"],
            default="1",
        )

        if choice.lower() == "n":
            alias = Prompt.ask("Alias for the new org", default="myorg")
            return alias if login_new_org(alias) else None

        chosen = orgs[int(choice) - 1]
        alias = chosen.get("alias")
        if alias:
            return alias
        # Org has no alias — assign one
        alias = Prompt.ask("This org has no alias. Pick one", default="myorg")
        subprocess.run(
            [_sf_exe(), "alias", "set", f"{alias}={chosen['username']}"],
            capture_output=True, timeout=10,
        )
        return alias

    console.print("[yellow]No connected orgs found.[/yellow]")
    alias = Prompt.ask("Alias for the new org", default="myorg")
    return alias if login_new_org(alias) else None


# ----------------------------------------------------------------------
# Step 3 — provider + API key
# ----------------------------------------------------------------------

def pick_provider() -> str:
    """Numbered menu of LLM providers."""
    table = Table(title="LLM providers")
    table.add_column("#", style="cyan", justify="right")
    table.add_column("Provider", style="bold")
    table.add_column("Free tier")
    table.add_column("API key URL", style="dim")
    for i, name in enumerate(PROVIDER_INFO, 1):
        info = PROVIDER_INFO[name]
        table.add_row(str(i), info["label"], info["free_tier"], info["key_url"])
    console.print(table)

    names = list(PROVIDER_INFO.keys())
    choice = Prompt.ask(
        "Pick a provider",
        choices=[str(i) for i in range(1, len(names) + 1)],
        default="1",
    )
    return names[int(choice) - 1]


def get_api_key(provider: str) -> str:
    info = PROVIDER_INFO[provider]
    console.print(
        f"\nGet a [bold]{info['label']}[/bold] API key from: "
        f"[cyan]{info['key_url']}[/cyan]"
    )
    return Prompt.ask(f"Paste your {info['key_env']}", password=True).strip()


def validate_api_key(provider: str, key: str) -> bool:
    """Make a tiny test call to confirm the key works."""
    if not key:
        return False
    import os
    os.environ[PROVIDER_INFO[provider]["key_env"]] = key

    console.print("[dim]Validating key with a test call...[/dim]")
    try:
        from sf_dev_agent.providers import create_provider
        p = create_provider(provider=provider)
        resp = p.chat(
            system="You are a test. Reply with the single word OK.",
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            max_tokens=10,
        )
        ok = bool(resp.text_blocks)
        console.print("[green]✓ Key works[/green]" if ok else "[red]✗ No reply[/red]")
        return ok
    except Exception as exc:
        console.print(f"[bold red]Validation failed:[/bold red] {exc}")
        return False


# ----------------------------------------------------------------------
# Step 4 — write .env
# ----------------------------------------------------------------------

def write_env_file(provider: str, api_key: str, org_alias: str) -> Path:
    """Write a minimal .env. Backs up existing file as .env.bak."""
    env_path = repo_root() / ".env"
    if env_path.exists():
        backup = repo_root() / ".env.bak"
        env_path.replace(backup)
        console.print(f"[dim]Existing .env backed up to {backup.name}[/dim]")

    key_var = PROVIDER_INFO[provider]["key_env"]
    contents = (
        "# Salesforce Developer Agent — generated by `setup` wizard\n"
        "# Everything else (instance URL, org type, API version, workspace) is\n"
        "# auto-detected at runtime. Add overrides here if needed.\n\n"
        f"LLM_PROVIDER={provider}\n"
        f"{key_var}={api_key}\n\n"
        f"SF_ORG_ALIAS={org_alias}\n"
    )
    env_path.write_text(contents, encoding="utf-8")
    return env_path


# ----------------------------------------------------------------------
# Orchestrator
# ----------------------------------------------------------------------

def run_setup() -> None:
    console.print(Panel(
        "[bold]sf-dev-agent setup wizard[/bold]\n\n"
        "I'll walk you through connecting an LLM and a Salesforce org. "
        "Most fields are auto-detected — you'll only need to paste an API key "
        "and pick an org.",
        border_style="cyan",
    ))

    console.print("\n[bold]1. Checking sf CLI...[/bold]")
    if not check_sf_cli():
        sys.exit(1)

    console.print("\n[bold]2. Pick a Salesforce org[/bold]")
    alias = pick_org()
    if not alias:
        console.print("[red]No org selected. Aborting.[/red]")
        sys.exit(1)
    console.print(f"[green]✓[/green] Using org alias [cyan]{alias}[/cyan]")

    console.print("\n[bold]3. Pick an LLM provider[/bold]")
    provider = pick_provider()

    console.print(f"\n[bold]4. {PROVIDER_INFO[provider]['label']} API key[/bold]")
    while True:
        key = get_api_key(provider)
        if validate_api_key(provider, key):
            break
        if not Confirm.ask("Try a different key?", default=True):
            console.print("[red]Aborting setup.[/red]")
            sys.exit(1)

    console.print("\n[bold]5. Writing .env[/bold]")
    env_path = write_env_file(provider, key, alias)
    console.print(f"[green]✓ Wrote {env_path}[/green]")

    console.print(Panel(
        "[bold green]Setup complete.[/bold green]\n\n"
        "Try a read-only run:\n"
        "  [cyan]uv run python -m sf_dev_agent \"List all Apex classes in the org\"[/cyan]\n\n"
        "Or jump straight into the REPL:\n"
        "  [cyan]uv run python -m sf_dev_agent[/cyan]",
        border_style="green",
    ))
