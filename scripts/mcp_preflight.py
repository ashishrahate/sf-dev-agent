"""Preflight for the cross-agent MCP test: what is ready and what is still missing.

Usage:
    uv run python scripts/mcp_preflight.py --org <alias>

Checks (each prints OK / MISSING with the fix):
  1. Salesforce CLI on PATH
  2. Org alias authenticated in the sf CLI
  3. Node + npx (needed to launch the Salesforce DX MCP server)
  4. Embedder resolves, and the local model is downloadable/loaded
  5. Context index built for the org, embedded, and not stale
  6. Knowledge base embedded
Exit code is the number of MISSING items.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "packages" / "sf-context-engine" / "src"))

_failures = 0


def report(ok: bool, label: str, detail: str = "", fix: str = "") -> None:
    global _failures
    print(f"[{'OK' if ok else 'MISSING':7s}] {label}" + (f" - {detail}" if detail else ""))
    if not ok:
        _failures += 1
        if fix:
            print(f"          fix: {fix}")


def _which(*names: str) -> str | None:
    for n in names:
        found = shutil.which(n)
        if found:
            return found
    return None


def check_sf_cli(org: str) -> None:
    sf = _which("sf", "sf.cmd")
    report(sf is not None, "Salesforce CLI (sf)", sf or "",
           "Install from https://developer.salesforce.com/tools/salesforcecli")
    if not sf:
        return
    try:
        out = subprocess.run(
            [sf, "org", "list", "--json"], capture_output=True, text=True, timeout=60,
        )
        data = json.loads(out.stdout or "{}").get("result", {})
        orgs = [o for k in data for o in data[k] if isinstance(o, dict)]
        match = [o for o in orgs if org in (o.get("alias"), o.get("username"))]
        report(bool(match), f"Org alias '{org}' authenticated",
               (match[0].get("instanceUrl", "") if match else f"{len(orgs)} orgs known"),
               f"sf org login web --alias {org}")
    except Exception as exc:  # noqa: BLE001 - preflight must never crash
        report(False, f"Org alias '{org}' authenticated", str(exc),
               f"sf org login web --alias {org}")


def check_node() -> None:
    node, npx = _which("node"), _which("npx", "npx.cmd")
    report(bool(node and npx), "Node + npx (for @salesforce/mcp)",
           "" if node and npx else "not found", "Install Node 20+ from https://nodejs.org")


def check_embedder() -> None:
    from sf_context_engine import create_embedder

    try:
        if importlib_has("fastembed"):
            os.environ.setdefault("SF_CONTEXT_EMBEDDER", "fastembed")
        emb = create_embedder()
        warm = getattr(emb, "warmup", None)
        if warm:
            warm()
        report(not emb.name.startswith("mock"), "Embedder", f"{emb.name} ({emb.dim}-d)",
               "Install the local model: uv sync --all-packages --all-extras, "
               "then: uv run sf-context-mcp --warmup")
    except Exception as exc:  # noqa: BLE001
        report(False, "Embedder", str(exc), "uv sync --all-packages --all-extras")


def importlib_has(mod: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(mod) is not None


def check_index(org: str) -> None:
    from sf_context_engine.service import ContextService

    status = ContextService(org_alias=org).index_status()
    built = status.get("index_built", False)
    report(built, "Context index built", status.get("last_built_at") or "",
           f"call build_metadata_index (or: uv run sf-agent --org-alias {org} then /index)")
    if built:
        report(not status.get("is_stale", True), "Index is fresh",
               f"age {status.get('age_seconds')}s", "re-run build_metadata_index")
        report(status.get("embedding_coverage_pct", 0) >= 95, "Components embedded",
               f"{status.get('embedding_coverage_pct')}% of {status.get('components_count')}",
               "call embed_metadata_index")
        if status.get("embedder_mismatch"):
            report(False, "Embedder matches index", status["embedder_mismatch"]["detail"],
                   "call embed_metadata_index with reset_embeddings=true")


def check_kb(org: str) -> None:
    from sf_context_engine.service import ContextService

    res = ContextService(org_alias=org).knowledge_search("soql in loop", limit=1)
    ok = res.get("match_count", 0) > 0
    report(ok, "Knowledge base embedded", "", "call embed_knowledge_base")


def main() -> int:
    p = argparse.ArgumentParser()
    env_org = os.environ.get("SF_ORG_ALIAS")
    p.add_argument("--org", default=env_org, required=not env_org)
    args = p.parse_args()
    check_sf_cli(args.org)
    check_node()
    check_embedder()
    check_index(args.org)
    check_kb(args.org)
    print(f"\n{_failures} item(s) missing" if _failures else "\nAll ready for cross-agent testing")
    return _failures


if __name__ == "__main__":
    raise SystemExit(main())
