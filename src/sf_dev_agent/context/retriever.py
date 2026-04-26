"""Retrieve org metadata to a local directory for parsing.

Wraps `sf project retrieve start --metadata <Type1,Type2,...>`. The returned
directory contains an sfdx-format `force-app/main/default/...` tree that
parsers walk for component sources.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class RetrieveResult:
    success: bool
    output_dir: Path           # the sfdx project directory the source landed in
    component_types: list[str]
    raw: dict                  # the parsed `sf` JSON payload
    error: str | None = None


def _sf_exe() -> str:
    return "sf.cmd" if sys.platform == "win32" else "sf"


def retrieve(
    org_alias: str,
    component_types: list[str],
    target_dir: Path,
    timeout: int = 600,
) -> RetrieveResult:
    """Run `sf project retrieve start` for the given component types.

    `target_dir` should be an existing directory; if it doesn't already contain
    an sfdx-project.json this function writes a minimal one so the sf CLI is
    happy. Source lands under `target_dir/force-app/main/default/...`.
    """
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    # sf CLI rejects retrieves when packageDirectories[0].path doesn't exist.
    (target_dir / "force-app" / "main" / "default").mkdir(parents=True, exist_ok=True)

    project_file = target_dir / "sfdx-project.json"
    if not project_file.exists():
        project_file.write_text(
            json.dumps({
                "packageDirectories": [{"path": "force-app", "default": True}],
                "name": "metadata-index-retrieve",
                "namespace": "",
                "sfdcLoginUrl": "https://login.salesforce.com",
                "sourceApiVersion": "62.0",
            }, indent=2),
            encoding="utf-8",
        )

    cmd = [_sf_exe(), "project", "retrieve", "start"]
    for ctype in component_types:
        cmd += ["--metadata", ctype]
    cmd += ["--target-org", org_alias, "--json"]
    logger.info("Retrieving metadata (cwd=%s): %s", target_dir, " ".join(cmd))

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(target_dir),
        )
    except subprocess.TimeoutExpired:
        return RetrieveResult(
            success=False,
            output_dir=target_dir,
            component_types=component_types,
            raw={},
            error=f"sf retrieve timed out after {timeout}s",
        )

    try:
        payload = json.loads(proc.stdout) if proc.stdout else {}
    except json.JSONDecodeError:
        return RetrieveResult(
            success=False,
            output_dir=target_dir,
            component_types=component_types,
            raw={"stdout": proc.stdout[-2000:], "stderr": proc.stderr[-2000:]},
            error="sf retrieve produced non-JSON output",
        )

    success = payload.get("status") == 0
    return RetrieveResult(
        success=success,
        output_dir=target_dir,
        component_types=component_types,
        raw=payload,
        error=None if success else (payload.get("message") or proc.stderr[-500:]),
    )
