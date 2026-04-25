"""Auto-derive Salesforce config from sf CLI and sfdx-project.json.

Centralizes logic that lets users set the bare minimum in .env (just the org
alias) while everything else — instance URL, org type, API version — is
discovered at runtime.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys

from sf_dev_agent.paths import agent_workspace

logger = logging.getLogger(__name__)

# Fallback API version if sfdx-project.json is missing or unreadable.
_FALLBACK_API_VERSION = "62.0"


def _sf_exe() -> str:
    return "sf.cmd" if sys.platform == "win32" else "sf"


def describe_org(alias: str) -> dict | None:
    """Return parsed `sf org display --target-org <alias> --json` result, or None."""
    try:
        proc = subprocess.run(
            [_sf_exe(), "org", "display", "--target-org", alias, "--json"],
            capture_output=True, text=True, timeout=30,
            cwd=str(agent_workspace()),
        )
        data = json.loads(proc.stdout) if proc.stdout else {}
        return data.get("result") if data.get("status") == 0 else None
    except (subprocess.SubprocessError, json.JSONDecodeError, FileNotFoundError):
        return None


def derive_org_type(alias: str, override: str | None = None) -> str:
    """Determine org type from `sf org display`. Returns override if provided."""
    if override:
        return override
    info = describe_org(alias)
    if not info:
        return "developer"
    if info.get("isScratch"):
        return "scratch"
    if info.get("isSandbox"):
        return "sandbox"
    if info.get("isDevHub"):
        return "developer"
    return "developer"


def derive_instance_url(alias: str, override: str | None = None) -> str:
    """Pull the live instance URL from sf CLI. Returns override if provided."""
    if override:
        return override
    info = describe_org(alias)
    return (info or {}).get("instanceUrl", "")


def derive_api_version(override: str | None = None) -> str:
    """Read sourceApiVersion from sfdx-project.json. Returns override if provided."""
    if override:
        return override
    project_file = agent_workspace() / "sfdx-project.json"
    try:
        data = json.loads(project_file.read_text(encoding="utf-8"))
        return str(data.get("sourceApiVersion") or _FALLBACK_API_VERSION)
    except (OSError, json.JSONDecodeError):
        return _FALLBACK_API_VERSION
