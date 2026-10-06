"""Cross-agent benchmark runner: headless Claude Code against sf-context and/or Salesforce DX MCP.

Usage:
    uv run python scripts/mcp_bench.py --list
    uv run python scripts/mcp_bench.py --tasks T1 T5 --configs A B C --setting repo
    uv run python scripts/mcp_bench.py --tasks T2 --configs A C --setting repo --allow-org-writes

Settings:
    repo  agent's working dir is a local DX project (retrieved copy of the org) and it may
          read/write files. Realistic "developer repo" baseline.
    org   empty working dir, no file tools: the agent must learn about the org through MCP.

Each run uses an isolated copy of the context DB, so memory writes and index refreshes never
leak between runs. Raw summaries go to docs/mcp/runs/. Tasks that write to the org (T2) need
--allow-org-writes and restore the org afterwards.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "docs" / "mcp" / "runs"
BENCH = Path(os.environ.get("MCP_BENCH_DIR", ROOT / ".bench"))
BASELINE = BENCH / "baseline"  # retrieved DX project (force-app/...)
ORG = os.environ.get("SF_ORG_ALIAS", "AgentforceOrg")
CLAUDE = shutil.which("claude") or "claude"
MAIN_DB = Path.home() / ".sf-context" / "metadata_index.db"

CTX = "mcp__sf-context"
DX = "mcp__salesforce-dx"


@dataclass
class Task:
    id: str
    prompt: str
    required: list[tuple[str, str]]  # (label, regex) all must appear in the final answer
    bonus: list[tuple[str, str]] = field(default_factory=list)
    forbidden: list[tuple[str, str]] = field(default_factory=list)
    configs: str = "ABC"
    writes_org: bool = False
    note: str = ""


TASKS: dict[str, Task] = {t.id: t for t in [
    Task(
        "T1",
        "List everything in this org that touches the Account object: triggers, flows, "
        "validation rules and the classes involved, and how they depend on each other.",
        required=[
            ("AccountTrigger", r"AccountTrigger\b"),
            ("AccountTriggerHandler", r"AccountTriggerHandler"),
            ("AccountService", r"AccountService"),
            ("AccountSelector", r"AccountSelector"),
            ("flow Append_Billing_Country", r"Append_Billing_Country"),
            ("chain trigger->handler->service->selector", r"(?s)AccountTrigger.*AccountTriggerHandler.*AccountService.*AccountSelector"),
        ],
        bonus=[
            ("says flow is draft/inactive", r"(?i)draft|inactive|not active"),
            ("says no validation rules exist", r"(?i)no (active |custom )?validation rules|zero validation rules|0 validation rules|validation rules?:?\s*(none|0)"),
        ],
        forbidden=[("invented validation rule", r"(?i)\b\w+_(?:VR|Rule|Validation)\b")],
    ),
    Task(
        "T2",
        "Add validation to AccountTriggerHandler so it rejects an Account with negative "
        "AnnualRevenue. Don't break existing callers, follow this org's trigger conventions, "
        "include a test class, and deploy it to the org.",
        required=[],  # graded from file/org diff, see grade_t2
        configs="ABC", writes_org=True,
        note="B has no deploy tool: graded on code only.",
    ),
    Task(
        "T3",
        "Review AccountTriggerHandler against Salesforce best practices. Which governor-limit "
        "or anti-pattern problems apply?",
        required=[
            ("looks at delegation to AccountService", r"AccountService"),
            ("bulk handling", r"(?i)bulk"),
            ("static bypass flag", r"(?i)bypass"),
        ],
        bonus=[("notes SOQL is in selector w/ security", r"(?i)SECURITY_ENFORCED|AccountSelector")],
        forbidden=[("claims SOQL/DML in loop in handler", r"(?i)(SOQL|DML)[^.\n]{0,40}(inside|in) (a |the )?(for )?loop[^.\n]{0,30}(handler|AccountTriggerHandler)")],
    ),
    Task(
        "T4a",
        "Record this decision for the team: we prefer Apex triggers with a handler class over "
        "Flows for complex Account logic, because the team owns the Apex skills.",
        required=[("confirms it was recorded/saved", r"(?i)sav|record|remember|stor|memory|noted|written")],
        configs="BC", note="Needs a persistent store: A has none (CLAUDE.md not used).",
    ),
    Task(
        "T4b",
        "I need to add logic on Account insert. How should I do it here?",
        required=[
            ("mentions trigger handler approach", r"(?i)handler"),
            ("cites the team's Apex-over-Flow preference", r"(?i)(prefer|decision|team|skills).{0,80}(apex|trigger)|(apex|trigger).{0,80}(prefer|decision|team|skills)"),
        ],
        configs="BC", note="Run after T4a in the same config (shared DB copy), new session.",
    ),
    Task(
        "T5",
        "Which classes call RecordSelectorController, and what would break if I rename it?",
        required=[
            ("recordSelector LWC", r"recordSelector"),
            ("test class", r"RecordSelectorControllerTest"),
            ("LWC import breaks", r"(?i)import|@salesforce/apex|wire"),
        ],
    ),
    Task(
        "T6",
        "Is your view of the org up to date? Refresh it if not, then tell me what changed.",
        required=[("names the changed class", r"AccountSelector"),
                  ("says it was refreshed/updated", r"(?i)refresh|re-?index|updated|up to date")],
        configs="BC", writes_org=True,
        note="Run only after the harness mutated AccountSelector in the org.",
    ),
]}


def mcp_config(cfg: str, db: Path) -> dict:
    servers: dict = {}
    if "B" in cfg or "C" in cfg:
        servers["sf-context"] = {
            "command": "uv",
            "args": ["--directory", str(ROOT), "run", "sf-context-mcp", "--org", ORG],
            "env": {"SF_CONTEXT_DB": str(db), "SF_CONTEXT_EMBEDDER": "fastembed"},
        }
    if "A" in cfg or "C" in cfg:
        servers["salesforce-dx"] = {
            "command": "cmd",
            "args": ["/c", "npx", "-y", "@salesforce/mcp", "--orgs", ORG,
                     "--toolsets", "orgs,data,metadata,testing"],
        }
    return {"mcpServers": servers}


def run_claude(task: Task, cfg: str, setting: str, model: str, rundir: Path) -> dict:
    workdir = rundir / "work"
    if setting == "repo":
        shutil.copytree(BASELINE, workdir)
    else:
        workdir.mkdir(parents=True)
    if task.id in ("T4a", "T4b"):
        # T4b must see what T4a saved: one DB per (config, setting), reset at T4a.
        db = BENCH / f"t4_{cfg}_{setting}.db"
        if task.id == "T4a" or not db.exists():
            shutil.copy(MAIN_DB, db)
    else:
        db = rundir / "metadata_index.db"
        shutil.copy(MAIN_DB, db)
    cfg_path = rundir / "mcp.json"
    cfg_path.write_text(json.dumps(mcp_config(cfg, db)), encoding="utf-8")

    allowed = []
    if "B" in cfg or "C" in cfg:
        allowed.append(CTX)
    if "A" in cfg or "C" in cfg:
        allowed.append(DX)
    builtin = "Read,Write,Edit,Glob,Grep" if setting == "repo" else ""
    if builtin:
        allowed += builtin.split(",")
    cmd = [
        CLAUDE, "-p", "--model", model, "--output-format", "stream-json", "--verbose",
        "--mcp-config", str(cfg_path), "--strict-mcp-config",
        "--tools", builtin, "--allowedTools", ",".join(allowed),
        "--permission-mode", "acceptEdits", "--setting-sources", "",
        "--disable-slash-commands", "--no-session-persistence",
        "--max-budget-usd", os.environ.get("MCP_BENCH_BUDGET", "1.5"),
    ]
    t0 = time.time()
    proc = subprocess.run(
        cmd, input=task.prompt, capture_output=True, text=True, cwd=workdir,
        timeout=int(os.environ.get("MCP_BENCH_TIMEOUT", "420")), encoding="utf-8", errors="replace",
    )
    wall = time.time() - t0
    (rundir / "stream.jsonl").write_text(proc.stdout, encoding="utf-8")
    return summarize(proc.stdout, proc.stderr, wall, workdir)


def summarize(stdout: str, stderr: str, wall: float, workdir: Path) -> dict:
    calls: list[str] = []
    result_chars = {"sf-context": 0, "salesforce-dx": 0, "builtin": 0}
    id_to_name: dict[str, str] = {}
    final: dict = {}
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "assistant":
            for blk in ev.get("message", {}).get("content", []):
                if blk.get("type") == "tool_use":
                    calls.append(blk["name"])
                    id_to_name[blk["id"]] = blk["name"]
        elif ev.get("type") == "user":
            content = ev.get("message", {}).get("content", [])
            for blk in content if isinstance(content, list) else []:
                if blk.get("type") == "tool_result":
                    name = id_to_name.get(blk.get("tool_use_id"), "")
                    body = blk.get("content")
                    n = len(body if isinstance(body, str) else json.dumps(body))
                    key = ("sf-context" if name.startswith(CTX) else
                           "salesforce-dx" if name.startswith(DX) else "builtin")
                    result_chars[key] += n
        elif ev.get("type") == "result":
            final = ev
    usage = final.get("usage", {})
    return {
        "answer": final.get("result", ""),
        "is_error": final.get("is_error", True) if final else True,
        "error_detail": (stderr[-400:] if not final else final.get("subtype", "")),
        "tool_calls": calls,
        "calls_ctx": sum(c.startswith(CTX) for c in calls),
        "calls_dx": sum(c.startswith(DX) for c in calls),
        "calls_builtin": sum(not c.startswith(("mcp__",)) for c in calls),
        "mcp_result_tokens_est": (result_chars["sf-context"] + result_chars["salesforce-dx"]) // 4,
        "result_chars": result_chars,
        "turns": final.get("num_turns"),
        "cost_usd": final.get("total_cost_usd"),
        "input_tokens": usage.get("input_tokens"),
        "cache_read_tokens": usage.get("cache_read_input_tokens"),
        "cache_creation_tokens": usage.get("cache_creation_input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "wall_s": round(wall, 1),
        "workdir": str(workdir),
    }


def grade_text(task: Task, answer: str) -> dict:
    req = {label: bool(re.search(rx, answer)) for label, rx in task.required}
    bonus = {label: bool(re.search(rx, answer)) for label, rx in task.bonus}
    forb = {label: bool(re.search(rx, answer)) for label, rx in task.forbidden}
    return {
        "required_hit": sum(req.values()), "required_total": len(req),
        "missing": [k for k, v in req.items() if not v],
        "bonus": bonus, "forbidden_flags": [k for k, v in forb.items() if v],
    }


# ---------------------------------------------------------------------------- org helpers


def sf(*args: str, timeout: int = 600, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run the sf CLI. Project commands (deploy/delete) must run inside a DX project dir."""
    exe = shutil.which("sf") or "sf"
    env = {**os.environ, "NO_COLOR": "1", "FORCE_COLOR": "0", "SF_NO_COLOR": "true"}
    return subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout,
                          encoding="utf-8", errors="replace", cwd=cwd or BASELINE, env=env)


def org_apex_names() -> set[str]:
    out = sf("data", "query", "--use-tooling-api", "--target-org", ORG, "--json", "-q",
             "SELECT Name FROM ApexClass WHERE NamespacePrefix = null")
    return {r["Name"] for r in json.loads(out.stdout)["result"]["records"]}


def restore_org(baseline_classes: set[str]) -> str:
    """Redeploy baseline classes/triggers and delete anything the agent added."""
    d = BASELINE / "force-app" / "main" / "default"
    r = sf("project", "deploy", "start", "--target-org", ORG, "--ignore-conflicts",
           "--source-dir", str(d / "classes"), "--source-dir", str(d / "triggers"),
           "--json", timeout=900)
    msg = "redeploy ok" if '"status": 0' in r.stdout else f"redeploy FAILED: {r.stdout[-300:]}"
    extra = org_apex_names() - baseline_classes
    for name in sorted(extra):
        sf("project", "delete", "source", "--target-org", ORG, "-m", f"ApexClass:{name}",
           "--no-prompt", "--json", timeout=600)
        msg += f"; deleted extra ApexClass:{name}"
    return msg


def baseline_class_names() -> set[str]:
    return {p.name.split(".")[0] for p in (BASELINE / "force-app/main/default/classes").glob("*.cls")}


def grade_t2(summary: dict, cfg: str) -> dict:
    """T2 is graded from what the agent actually produced and from the org."""
    work = Path(summary["workdir"])
    base = BASELINE / "force-app" / "main" / "default"
    changed, new = [], []
    cur = work / "force-app" / "main" / "default"
    if cur.exists():
        for f in list(cur.rglob("*.cls")) + list(cur.rglob("*.trigger")):
            rel = f.relative_to(cur)
            if not (base / rel).exists():
                new.append(str(rel))
            elif f.read_text(encoding="utf-8", errors="replace") != (base / rel).read_text(encoding="utf-8", errors="replace"):
                changed.append(str(rel))
    text = "\n".join((cur / p).read_text(encoding="utf-8", errors="replace") for p in changed + new
                     if (cur / p).exists())
    ans = summary["answer"]
    checks = {
        "code references AnnualRevenue": "AnnualRevenue" in text,
        "rejects negative (< 0 / addError)": bool(re.search(r"AnnualRevenue\s*<\s*0", text)) and "addError" in text,
        "no SOQL/DML added in loop (heuristic)": not re.search(r"for\s*\([^)]*\)\s*\{[^}]*\[\s*SELECT", text),
        "has test coverage change": any("test" in p.lower() for p in changed + new)
                                   or bool(re.search(r"@IsTest", text, re.I)),
        "followed existing structure (edited handler/service, no 2nd trigger)": not any(
            p.endswith(".trigger") for p in new),
        "claims deployed": bool(re.search(r"(?i)deploy(ed)? (succe|to)|successfully deployed", ans)),
    }
    return {"changed_files": changed, "new_files": new, "checks": checks,
            "required_hit": sum(checks.values()), "required_total": len(checks)}


def mutate_for_t6() -> str:
    """Deploy a harmless change to AccountSelector so a prior index becomes stale."""
    work = BENCH / "t6mut"
    if work.exists():
        shutil.rmtree(work)
    shutil.copytree(BASELINE, work)
    f = work / "force-app/main/default/classes/AccountSelector.cls"
    f.write_text(f.read_text(encoding="utf-8").rstrip() + "\n// bench-marker: stale-index test\n",
                 encoding="utf-8")
    r = sf("project", "deploy", "start", "--target-org", ORG, "--ignore-conflicts",
           "--source-dir", str(f.parent / "AccountSelector.cls"), "--json", timeout=600, cwd=work)
    return "mutated" if '"status": 0' in r.stdout else f"mutation FAILED {r.stdout[-300:]}"


# ---------------------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", nargs="*", default=[])
    ap.add_argument("--configs", nargs="*", default=["A", "B", "C"])
    ap.add_argument("--setting", choices=["repo", "org"], default="repo")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--allow-org-writes", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--tag", default="")
    args = ap.parse_args()
    if args.list:
        for t in TASKS.values():
            print(f"{t.id:4s} configs={t.configs:3s} writes_org={t.writes_org}  {t.prompt[:90]}")
        return 0
    RUNS.mkdir(parents=True, exist_ok=True)
    base_classes = baseline_class_names()
    rows = []
    for tid in args.tasks:
        task = TASKS[tid]
        if task.writes_org and not args.allow_org_writes:
            print(f"skip {tid}: writes to the org (pass --allow-org-writes)")
            continue
        if tid == "T6":
            print("T6 mutation:", mutate_for_t6())
        for cfg in args.configs:
            if cfg not in task.configs:
                print(f"skip {tid}/{cfg}: n/a ({task.note})")
                continue
            stamp = time.strftime("%H%M%S")
            rundir = BENCH / "runs" / f"{tid}_{cfg}_{args.setting}_{stamp}"
            rundir.mkdir(parents=True)
            print(f"== {tid} config {cfg} ({args.setting}) ...", flush=True)
            try:
                s = run_claude(task, cfg, args.setting, args.model, rundir)
            except subprocess.TimeoutExpired:
                s = {"answer": "", "is_error": True, "error_detail": "timeout", "tool_calls": [],
                     "workdir": str(rundir / "work")}
            g = grade_t2(s, cfg) if tid == "T2" else grade_text(task, s["answer"])
            if tid == "T2" and cfg in ("A", "C"):
                s["org_restore"] = restore_org(base_classes)
            row = {"task": tid, "config": cfg, "setting": args.setting, "model": args.model,
                   "tag": args.tag, **{k: v for k, v in s.items() if k != "workdir"}, "grade": g}
            (RUNS / f"{tid}_{cfg}_{args.setting}{args.tag}.json").write_text(
                json.dumps(row, indent=2), encoding="utf-8")
            rows.append(row)
            print(f"   {g['required_hit']}/{g['required_total']} | ctx={s.get('calls_ctx')} "
                  f"dx={s.get('calls_dx')} builtin={s.get('calls_builtin')} turns={s.get('turns')} "
                  f"cost=${s.get('cost_usd')} wall={s.get('wall_s')}s err={s['is_error']}")
        if tid == "T6":
            print("T6 restore:", restore_org(base_classes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
