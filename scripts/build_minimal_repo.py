"""Build a minimal distribution variant of sf-dev-agent.

Walks the repo from the root, copies an allowlist of paths into an output
directory, and writes a stripped-down README + .env.example for that
bundle. The result is a self-sufficient checkout suitable for sharing
externally without ROADMAP / ARCHITECTURE / session logs / memory state.

Usage:
    python scripts/build_minimal_repo.py [--out dist/sf-dev-agent-min] [--no-validate]

The build is purely additive: the source repo is never modified. `--validate`
(default on) runs `uv sync --frozen` + `uv run pytest --collect-only -q`
inside the output directory to confirm the bundle is self-sufficient.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

# Allowlist of paths copied verbatim into the output directory. Each entry
# is interpreted relative to the source repo root. Directories are copied
# recursively; files are copied as-is. Globs are NOT used — explicit paths
# keep the manifest auditable.
INCLUDE_PATHS: tuple[str, ...] = (
    "src",
    "tests",
    "pyproject.toml",
    "uv.lock",
)

# Allowlisted paths that may or may not exist (e.g. LICENSE before one is
# added). Missing entries are skipped silently — present entries are
# copied like INCLUDE_PATHS.
OPTIONAL_INCLUDE_PATHS: tuple[str, ...] = (
    "LICENSE",
)

# Top-level directories that the build script writes from templates rather
# than copying. Listed separately so the validate step can assert their
# presence by name without depending on the copy path.
TEMPLATED_FILES: tuple[str, ...] = (
    "README.md",
    ".env.example",
)

# Items deliberately excluded — the entire point of the minimal variant.
# Used by the test suite to assert the build script didn't leak anything.
EXCLUDED_PATHS: tuple[str, ...] = (
    "docs",
    "ROADMAP.md",
    "PRESSURE_TEST.md",
    "ARCHITECTURE.md",
    "PROJECT_SUMMARY.md",
    "memory",
    ".claude",
    "workspace",
    "reports",
    "main.py",
)


MINIMAL_README = """\
# sf-dev-agent (minimal distribution)

Autonomous AI agent for Salesforce platform development. This is the
**minimal distribution variant** — source, tests, lockfile, and license
only. See the full repo for ROADMAP, architecture notes, and design
history: https://github.com/anthropics/sf-dev-agent

## Install

```
uv sync
```

## First-run checks

```
uv run sf-agent doctor          # prereq audit (python / node / sf CLI / git / API key)
uv run sf-agent setup           # one-shot interactive .env wizard
```

## Launch

```
uv run sf-agent                 # persistent REPL — Shift+Tab cycles operating modes
uv run sf-agent "create an Apex trigger handler for Account"
                                # one-shot mode
```

## Subcommands

| Command | What it does |
|---|---|
| `sf-agent` | Persistent REPL with slash commands. |
| `sf-agent setup` | Interactive .env wizard. |
| `sf-agent doctor` | Probe system prerequisites. |
| `sf-agent resume <task-id>` | Pick up a persisted task. |
| `sf-agent memory <extract\\|export\\|promote>` | Memory tier maintenance. |
| `sf-agent audit tokens [--by tool\\|model] [--since 7d]` | LLM token usage report. |

## Tests

```
uv run pytest                   # default suite (no live org needed)
uv run pytest -m integration    # opt-in: live-org tests
```

## License

See LICENSE.
"""


MINIMAL_ENV_EXAMPLE = """\
# Salesforce Developer Agent — environment variables (minimal distribution)
#
# Easiest path: run the wizard instead of editing by hand.
#   uv run sf-agent setup
# ─────────────────────────────────────────────────────────────────────────────

# ── REQUIRED: pick ONE provider and set its key ─────────────────────────────
# LLM_PROVIDER=gemini            # anthropic | openai | gemini (auto-detected)

ANTHROPIC_API_KEY=
OPENAI_API_KEY=
GOOGLE_API_KEY=

# Optional: model override.
# LLM_MODEL=

# ── REQUIRED: which Salesforce org to target ────────────────────────────────
# Must match an alias from `sf org list`.
SF_ORG_ALIAS=

# ── Logging ─────────────────────────────────────────────────────────────────
LOG_LEVEL=INFO
"""


def build_minimal_repo(
    source_root: Path,
    out_dir: Path,
    *,
    overwrite: bool = True,
) -> dict[str, list[str]]:
    """Copy the allowlist into `out_dir` and write the bundled templates.

    Returns a manifest of what landed: {"copied": [...], "written": [...]}.
    Raises FileNotFoundError if `source_root` is missing an allowlisted path.
    """
    source_root = source_root.resolve()
    out_dir = out_dir.resolve()
    if not source_root.is_dir():
        raise FileNotFoundError(f"Source root not found: {source_root}")

    if out_dir.exists():
        if not overwrite:
            raise FileExistsError(
                f"{out_dir} already exists; pass overwrite=True to replace."
            )
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    def _copy_one(rel: str, required: bool) -> bool:
        src = source_root / rel
        if not src.exists():
            if required:
                raise FileNotFoundError(
                    f"Allowlisted path missing in source repo: {rel}"
                )
            return False
        dst = out_dir / rel
        if src.is_dir():
            shutil.copytree(
                src, dst,
                ignore=shutil.ignore_patterns(
                    "__pycache__", "*.pyc", ".pytest_cache",
                ),
            )
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        return True

    copied: list[str] = []
    for rel in INCLUDE_PATHS:
        _copy_one(rel, required=True)
        copied.append(rel)
    for rel in OPTIONAL_INCLUDE_PATHS:
        if _copy_one(rel, required=False):
            copied.append(rel)

    written: list[str] = []
    (out_dir / "README.md").write_text(MINIMAL_README, encoding="utf-8")
    written.append("README.md")
    (out_dir / ".env.example").write_text(MINIMAL_ENV_EXAMPLE, encoding="utf-8")
    written.append(".env.example")

    return {"copied": copied, "written": written}


def validate_bundle(out_dir: Path) -> tuple[bool, list[str]]:
    """Smoke-check the output bundle. Returns (ok, log_lines).

    Runs `uv sync --frozen` and `uv run pytest -q --collect-only` inside
    `out_dir`. Either step's non-zero exit flips `ok=False` and the captured
    output joins the log so the caller can surface it.
    """
    log: list[str] = []

    def run(cmd: list[str]) -> int:
        log.append(f"$ {' '.join(cmd)}")
        proc = subprocess.run(
            cmd, cwd=out_dir, capture_output=True, text=True, check=False,
        )
        log.append(proc.stdout.strip())
        if proc.stderr.strip():
            log.append(proc.stderr.strip())
        return proc.returncode

    sync_rc = run(["uv", "sync", "--frozen"])
    if sync_rc != 0:
        return False, log
    collect_rc = run(["uv", "run", "pytest", "-q", "--collect-only"])
    return collect_rc == 0, log


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="build_minimal_repo",
        description="Produce a minimal distribution of sf-dev-agent.",
    )
    parser.add_argument(
        "--source",
        default=str(Path(__file__).resolve().parent.parent),
        help="Source repo root (default: this repo).",
    )
    parser.add_argument(
        "--out",
        default="dist/sf-dev-agent-min",
        help="Output directory (default: dist/sf-dev-agent-min relative to source).",
    )
    parser.add_argument(
        "--no-validate", action="store_true",
        help="Skip the `uv sync` + `pytest --collect-only` smoke check.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    source = Path(args.source).resolve()
    out = Path(args.out)
    if not out.is_absolute():
        out = source / out

    manifest = build_minimal_repo(source, out)
    print(f"Wrote {len(manifest['copied'])} top-level paths to {out}")
    for rel in manifest["copied"]:
        print(f"  copied: {rel}")
    for rel in manifest["written"]:
        print(f"  templated: {rel}")

    if args.no_validate:
        print("Skipped validation (--no-validate).")
        return 0

    ok, log = validate_bundle(out)
    print("\n".join(log))
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
