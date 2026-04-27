"""System-prerequisite check + assisted-install hints.

Phase B.1 of the post-Wave-8 roadmap. `sf-agent doctor` probes for the
tools the agent needs at runtime (Python / uv / Node / sf CLI / git) and
the LLM-API-key configuration. Reports a `rich` table of green / yellow /
red with the exact install command for each missing item on the user's
detected OS.

Two modes:
    `sf-agent doctor`           pure check, prints the table.
    `sf-agent doctor --install` prints the install commands for missing
                                 items in copy-paste form. v1 does NOT
                                 auto-run them — admin/sudo handling and
                                 cross-platform UAC quirks are best left
                                 to the user. v2 may add an opt-in
                                 attempt path.

Wired into:
    - `sf-agent doctor` via a special-case dispatch in `__main__.py`
      (mirrors `setup` and `memory`).
    - The setup wizard, which runs `doctor` first and refuses to proceed
      if any *required* check fails.

Public API:
    run_all_checks(os_name=None) -> list[CheckResult]
    render_results(results)      -> rich.table.Table
    all_required_passing(results) -> bool
    doctor(install=False)        -> int           # exit-code-friendly
    main(argv=None)              -> int           # CLI entry point
"""

from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass
from enum import StrEnum

from rich.console import Console
from rich.table import Table

console = Console()


class Status(StrEnum):
    OK = "ok"
    OUTDATED = "outdated"   # binary found but version below the floor
    MISSING = "missing"     # binary not on PATH at all
    ERROR = "error"         # probe blew up for a non-version reason


@dataclass
class CheckResult:
    name: str
    status: Status
    version: str | None
    rationale: str
    required: bool
    install_command: str | None  # None when status is OK
    detail: str | None = None    # extra context shown under Notes/Fix


# ---------------------------------------------------------------------------
# OS detection + version parsing
# ---------------------------------------------------------------------------

def detect_os() -> str:
    """One of: 'windows' | 'macos' | 'linux' | 'other'."""
    p = platform.system()
    if p == "Windows":
        return "windows"
    if p == "Darwin":
        return "macos"
    if p == "Linux":
        return "linux"
    return "other"


def parse_version(text: str) -> tuple[int, ...] | None:
    """Find the first X.Y or X.Y.Z token in the text.

    Lenient: tools' --version output varies wildly. The first numeric
    triple in the string is overwhelmingly the version we want.
    """
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text)
    if not m:
        return None
    return tuple(int(x) for x in m.groups() if x is not None)


def meets_min(found: tuple[int, ...] | None, minimum: tuple[int, ...] | None) -> bool:
    if minimum is None:
        return True
    if found is None:
        return False
    pad = max(len(found), len(minimum))
    a = found + (0,) * (pad - len(found))
    b = minimum + (0,) * (pad - len(minimum))
    return a >= b


# ---------------------------------------------------------------------------
# Tool probes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ToolProbe:
    name: str
    binary: str
    version_args: tuple[str, ...]
    min_version: tuple[int, ...] | None
    required: bool
    rationale: str
    install_per_os: dict[str, str]


# Order matters — table renders in this sequence.
TOOL_PROBES: tuple[ToolProbe, ...] = (
    ToolProbe(
        name="Python 3.12+",
        binary="python",
        version_args=("--version",),
        min_version=(3, 12),
        required=True,
        rationale="Runs the agent.",
        install_per_os={
            "windows": "winget install Python.Python.3.12",
            "macos":   "brew install python@3.12",
            "linux":   "sudo apt-get install python3.12",
            "other":   "https://www.python.org/downloads/",
        },
    ),
    ToolProbe(
        name="uv",
        binary="uv",
        version_args=("--version",),
        min_version=(0, 4),
        required=True,
        rationale="Python dependency manager + script runner.",
        install_per_os={
            "windows": 'powershell -c "irm https://astral.sh/uv/install.ps1 | iex"',
            "macos":   "curl -LsSf https://astral.sh/uv/install.sh | sh",
            "linux":   "curl -LsSf https://astral.sh/uv/install.sh | sh",
            "other":   "https://docs.astral.sh/uv/getting-started/installation/",
        },
    ),
    ToolProbe(
        name="Node 18+",
        binary="node",
        version_args=("--version",),
        min_version=(18, 0),
        required=True,
        rationale="Runtime for the Salesforce CLI.",
        install_per_os={
            "windows": "winget install OpenJS.NodeJS.LTS",
            "macos":   "brew install node",
            "linux":   "sudo apt-get install nodejs npm",
            "other":   "https://nodejs.org/",
        },
    ),
    ToolProbe(
        name="Salesforce CLI (sf)",
        binary="sf",
        version_args=("--version",),
        min_version=(2, 0),
        required=True,
        rationale="Talks to your Salesforce org.",
        install_per_os={
            "windows": "npm install -g @salesforce/cli",
            "macos":   "npm install -g @salesforce/cli",
            "linux":   "npm install -g @salesforce/cli",
            "other":   "npm install -g @salesforce/cli",
        },
    ),
    ToolProbe(
        name="git",
        binary="git",
        version_args=("--version",),
        min_version=(2, 20),
        required=False,  # nice-to-have for project layout, not load-bearing at runtime
        rationale="Used for project layout + Markdown export round-trips.",
        install_per_os={
            "windows": "winget install Git.Git",
            "macos":   "brew install git",
            "linux":   "sudo apt-get install git",
            "other":   "https://git-scm.com/downloads",
        },
    ),
)


def probe_tool(probe: ToolProbe, os_name: str) -> CheckResult:
    """Run `<binary> <version_args>`. Map to a CheckResult."""
    located = shutil.which(probe.binary)
    if located is None:
        return CheckResult(
            name=probe.name,
            status=Status.MISSING,
            version=None,
            rationale=probe.rationale,
            required=probe.required,
            install_command=probe.install_per_os.get(os_name)
            or probe.install_per_os.get("other"),
        )

    try:
        proc = subprocess.run(
            [located, *probe.version_args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return CheckResult(
            name=probe.name,
            status=Status.ERROR,
            version=None,
            rationale=probe.rationale,
            required=probe.required,
            install_command=None,
            detail=f"{type(exc).__name__}: {exc}",
        )

    output = (proc.stdout or "") + (proc.stderr or "")
    parsed = parse_version(output)
    version_str = ".".join(str(x) for x in parsed) if parsed else None

    if parsed is None:
        # Binary responded but didn't print a version we recognize. Treat
        # as OK rather than failing — the binary exists and ran cleanly.
        first_line = (output.strip().splitlines() or [""])[0] or None
        return CheckResult(
            name=probe.name,
            status=Status.OK,
            version=first_line,
            rationale=probe.rationale,
            required=probe.required,
            install_command=None,
        )

    if meets_min(parsed, probe.min_version):
        return CheckResult(
            name=probe.name,
            status=Status.OK,
            version=version_str,
            rationale=probe.rationale,
            required=probe.required,
            install_command=None,
        )

    return CheckResult(
        name=probe.name,
        status=Status.OUTDATED,
        version=version_str,
        rationale=probe.rationale,
        required=probe.required,
        install_command=probe.install_per_os.get(os_name)
        or probe.install_per_os.get("other"),
        detail=(
            f"need >= {'.'.join(str(x) for x in probe.min_version)}"
            if probe.min_version else None
        ),
    )


# ---------------------------------------------------------------------------
# LLM-key check
# ---------------------------------------------------------------------------

LLM_KEY_LABELS: dict[str, str] = {
    "GOOGLE_API_KEY":    "Gemini",
    "OPENAI_API_KEY":    "OpenAI",
    "ANTHROPIC_API_KEY": "Anthropic",
}


def check_llm_key() -> CheckResult:
    """At least one of the three provider keys must be set + non-empty."""
    set_keys = [
        (env, label)
        for env, label in LLM_KEY_LABELS.items()
        if os.environ.get(env, "").strip()
    ]
    if not set_keys:
        return CheckResult(
            name="LLM API key",
            status=Status.MISSING,
            version=None,
            rationale="The agent's brain — at least one provider key is required.",
            required=True,
            install_command=(
                "Run `sf-agent setup`, OR set one of "
                "GOOGLE_API_KEY (free tier) / OPENAI_API_KEY / "
                "ANTHROPIC_API_KEY in .env"
            ),
        )
    detected = ", ".join(label for _, label in set_keys)
    return CheckResult(
        name="LLM API key",
        status=Status.OK,
        version=detected,
        rationale="The agent's brain.",
        required=True,
        install_command=None,
    )


# ---------------------------------------------------------------------------
# Run + render + verdict
# ---------------------------------------------------------------------------

def run_all_checks(os_name: str | None = None) -> list[CheckResult]:
    """Run every probe + the LLM-key check. Returns results in display order."""
    if os_name is None:
        os_name = detect_os()
    results = [probe_tool(p, os_name) for p in TOOL_PROBES]
    results.append(check_llm_key())
    return results


def render_results(results: list[CheckResult]) -> Table:
    """Compose a `rich` table the doctor prints to stdout."""
    table = Table(title="sf-agent prerequisites", header_style="bold cyan")
    table.add_column("Tool", style="bold")
    table.add_column("Status")
    table.add_column("Version")
    table.add_column("Notes / Fix", overflow="fold")

    for r in results:
        if r.status == Status.OK:
            status_cell = "[green]ok[/green]"
        elif r.status == Status.OUTDATED:
            status_cell = "[yellow]outdated[/yellow]"
        elif r.status == Status.MISSING:
            status_cell = "[red]missing[/red]" if r.required else "[yellow]missing[/yellow]"
        else:
            status_cell = "[red]error[/red]"

        if r.status == Status.OK:
            notes = r.rationale
        else:
            notes = r.install_command or r.rationale
        if r.detail:
            notes = f"{notes}\n[dim]{r.detail}[/dim]"

        table.add_row(
            r.name,
            status_cell,
            r.version or "—",
            notes or "—",
        )
    return table


def all_required_passing(results: list[CheckResult]) -> bool:
    """True iff every required check is OK."""
    return all(r.status == Status.OK for r in results if r.required)


def doctor(install: bool = False) -> int:
    """Run the full check, print the table, return an exit code.

    Exit codes:
        0 — every required check is OK.
        1 — at least one required check failed.

    Optional checks (e.g. `git`) failing do NOT flip the exit code; they
    show as yellow in the table but `sf-agent setup` will still proceed.
    """
    os_name = detect_os()
    results = run_all_checks(os_name)
    console.print(render_results(results))

    failing = [r for r in results if r.status != Status.OK]
    if not failing:
        console.print("[bold green]All prerequisites are good.[/bold green]")
        return 0

    if install:
        console.print(
            "\n[bold yellow]--install:[/bold yellow] copy-paste the commands "
            "below in a shell with appropriate privileges. Some require "
            "admin/sudo. v1 does not auto-run them."
        )
        for r in failing:
            if r.install_command:
                console.print(f"\n[bold]{r.name}[/bold]\n  {r.install_command}")

    if all_required_passing(results):
        console.print(
            "\n[dim]Required prerequisites are OK; only optional items "
            "are missing.[/dim]"
        )
        return 0
    return 1


def main(argv: list[str] | None = None) -> int:
    """CLI entry — invoked from `__main__.py`'s special-case dispatch."""
    parser = argparse.ArgumentParser(
        prog="sf-agent doctor",
        description="System prerequisite check + assisted install hints.",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help=(
            "Print the install commands for missing items. v1 prints "
            "only — does not auto-run."
        ),
    )
    args = parser.parse_args(argv)
    return doctor(install=args.install)
