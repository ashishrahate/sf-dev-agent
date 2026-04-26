"""Live-org smoke test for the agent loop.

Two modes:

  1. Pytest-driven (gated by `pytest -m smoke`)
        Runs a canned task against the configured org, auto-rejects the
        approval gate, and asserts the agent reached Phase 1 and emitted at
        least one tool call. Burns LLM tokens — opt-in only.

  2. Ad-hoc script
        $ uv run python tests/smoke_test_agent.py "<your task>"
        Drives the agent through Phase 1 with the supplied task, prints a
        per-call trace and a tool-usage summary so you can see whether the
        agent picks the new index-backed tools naturally.

Both modes auto-reject the approval gate so nothing is ever deployed.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

import pytest
from dotenv import load_dotenv

from sf_dev_agent.agent import AgentLoop
from sf_dev_agent.models.schemas import OrgConnection, Task, TaskStatus
from sf_dev_agent.providers import create_provider
from sf_dev_agent.sf_config import describe_org, derive_api_version, derive_org_type

DEFAULT_TASK = (
    "Tell me about the AccountTrigger in this org. What object is it on, what "
    "events does it fire on, and what classes does it call into? Then list "
    "every Apex class whose name contains 'AccountHandler'."
)


# ---------------------------------------------------------------------------
# Shared harness
# ---------------------------------------------------------------------------

def _build_org_from_env() -> OrgConnection | None:
    load_dotenv()
    alias = os.environ.get("SF_TEST_ORG_ALIAS") or os.environ.get("SF_ORG_ALIAS")
    if not alias:
        return None
    info = describe_org(alias)
    if not info:
        return None
    return OrgConnection(
        tenant_id="smoke-test",
        org_alias=alias,
        org_type=derive_org_type(alias),
        instance_url=info.get("instanceUrl", ""),
        api_version=derive_api_version(),
    )


def run_smoke(task: str, max_iterations: int = 15) -> tuple[Task, list[dict[str, Any]]]:
    """Drive the agent through Phase 1, auto-reject approval, return (task, trace)."""
    org = _build_org_from_env()
    if org is None:
        raise RuntimeError(
            "No connected org. Set SF_TEST_ORG_ALIAS or SF_ORG_ALIAS in .env."
        )

    provider = create_provider()
    agent = AgentLoop(org=org, provider=provider, max_iterations=max_iterations)

    trace: list[dict[str, Any]] = []
    original_execute_tool = agent._execute_tool

    def traced_execute(tool_name, tool_input, tool_use_id, phase):
        trace.append({"phase": phase, "tool": tool_name, "input": tool_input})
        return original_execute_tool(tool_name, tool_input, tool_use_id, phase)

    agent._execute_tool = traced_execute  # type: ignore[assignment]
    agent._request_approval = lambda: False  # type: ignore[assignment]

    result = agent.run(task)
    return result, trace


# ---------------------------------------------------------------------------
# Pytest entry — opt-in via `pytest -m smoke`
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_agent_phase1_runs_against_live_org_and_emits_tool_calls() -> None:
    """Sanity check: agent reaches Phase 1 and uses at least one tool."""
    org = _build_org_from_env()
    if org is None:
        pytest.skip("No connected org (SF_TEST_ORG_ALIAS / SF_ORG_ALIAS not set)")

    result, trace = run_smoke(DEFAULT_TASK)

    assert result.status in (TaskStatus.FAILED, TaskStatus.COMPLETE), \
        f"Unexpected terminal status: {result.status}"
    assert len(trace) > 0, "Agent emitted no tool calls during planning"
    # We don't hard-assert that code_search was called — the agent might solve
    # the task with sf_metadata_describe or other tools. That's information,
    # not a failure. Use the script entrypoint to inspect the trace.


# ---------------------------------------------------------------------------
# Script entry — ad-hoc exploration
# ---------------------------------------------------------------------------

def _print_trace(trace: list[dict[str, Any]]) -> None:
    print("\n\n=== TOOL CALL TRACE ===")
    for i, t in enumerate(trace, 1):
        inp = json.dumps(t["input"], default=str)
        if len(inp) > 220:
            inp = inp[:217] + "..."
        print(f"{i:2}. [{t['phase']:9}] {t['tool']:24} {inp}")

    counts: dict[str, int] = {}
    for t in trace:
        counts[t["tool"]] = counts.get(t["tool"], 0) + 1
    print("\n=== TOOL CALL COUNTS ===")
    for tool, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {tool:24} {n}")


def _main() -> None:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    task = " ".join(sys.argv[1:]) or DEFAULT_TASK
    org = _build_org_from_env()
    if org is None:
        print("ERROR: No connected org. Set SF_ORG_ALIAS in .env.", file=sys.stderr)
        sys.exit(1)

    print(f"\n[smoke] Org   : {org.org_alias} ({org.org_type})")
    print(f"[smoke] Task  : {task}\n")

    result, trace = run_smoke(task)
    _print_trace(trace)

    print(f"\n=== TASK STATUS ===\n{result.status.value}")
    if result.plan:
        print(f"plan_steps: {len(result.plan.steps)}")
        print(f"plan_risk:  {result.plan.risk_assessment.value}")


if __name__ == "__main__":
    _main()
