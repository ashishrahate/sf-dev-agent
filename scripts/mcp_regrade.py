"""Regrade stored benchmark runs with the current rubrics and print aggregate tables.

Usage:
    MCP_BENCH_DIR=<bench dir> uv run python scripts/mcp_regrade.py [--write]

Text tasks are regraded from the stored answer. T2 is regraded from the files the agent
produced (run directories under <bench dir>/runs, matched to repetitions in time order).
--write rewrites the `grade` field in docs/mcp/runs/*.json.

Repetition tags: "" = repetition 1, "_rep2", "_rep3". T6 repetition 1 is "_r2" (the first
T6 attempt, "_attempt1", was invalid and is ignored).
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mcp_bench as b  # noqa: E402

REP_OF_TAG = {"": 1, "_r2": 1, "_rep2": 2, "_rep3": 3}


def t2_dirs() -> dict[tuple[str, int], Path]:
    out: dict[tuple[str, int], Path] = {}
    for cfg in "ABC":
        dirs = sorted(glob.glob(str(b.BENCH / "runs" / f"T2_{cfg}_repo_*")), key=os.path.getmtime)
        for i, d in enumerate(dirs, start=1):
            out[(cfg, i)] = Path(d) / "work"
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()
    dirs = t2_dirs()
    rows = []
    for f in sorted(glob.glob(str(b.RUNS / "*.json"))):
        r = json.loads(Path(f).read_text(encoding="utf-8"))
        tag = r.get("tag", "")
        if tag in ("_pilot",) or f.endswith("_attempt1.json"):
            continue
        if r["task"] == "T6" and tag == "":
            continue  # superseded by _r2 (repetition 1) / _rep2 / _rep3
        rep = REP_OF_TAG.get(tag)
        if rep is None:
            continue
        task = b.TASKS[r["task"]]
        if r["task"] == "T2":
            work = dirs.get((r["config"], rep))
            if work is None or not work.exists():
                continue
            summary = {"workdir": str(work), "answer": r["answer"], "tool_calls": r["tool_calls"]}
            grade = b.grade_t2(summary, r["config"])
        else:
            grade = b.grade_text(task, r["answer"])
        r["grade"] = grade
        if args.write:
            Path(f).write_text(json.dumps(r, indent=2), encoding="utf-8")
        rows.append((r, rep, grade))

    def ok(g: dict) -> bool:
        return g["required_hit"] == g["required_total"] and not g.get("forbidden_flags")

    print("| Task | Setting | Cfg | Rep1 | Rep2 | Rep3 | Wrong-claim flags |")
    print("|---|---|---|---|---|---|---|")
    cells: dict[tuple, dict[int, str]] = collections.defaultdict(dict)
    flags: dict[tuple, list[str]] = collections.defaultdict(list)
    for r, rep, g in rows:
        key = (r["task"], r["setting"], r["config"])
        cells[key][rep] = f"{g['required_hit']}/{g['required_total']}" + ("" if ok(g) else "*")
        for fl in g.get("forbidden_flags", []):
            flags[key].append(f"rep{rep}: {fl}")
    order = {"T1": 1, "T2": 2, "T3": 3, "T4a": 4, "T4b": 5, "T5": 6, "T6": 7}
    for key in sorted(cells, key=lambda k: (order[k[0]], k[1], k[2])):
        c = cells[key]
        print(f"| {key[0]} | {key[1]} | {key[2]} | {c.get(1, '-')} | {c.get(2, '-')} | "
              f"{c.get(3, '-')} | {'; '.join(flags[key]) or '-'} |")

    print("\nAggregates, read-only tasks (T1, T3, T5), mean per task over all repetitions")
    print("| Setting | Cfg | n | MCP calls | Turns | MCP result tok | Cost $ | Wall s |")
    print("|---|---|---|---|---|---|---|---|")
    agg: dict[tuple, list[dict]] = collections.defaultdict(list)
    for r, _rep, _g in rows:
        if r["task"] in ("T1", "T3", "T5"):
            agg[(r["setting"], r["config"])].append(r)
    for (setting, cfg), rs in sorted(agg.items()):
        def m(k, rs=rs):
            return statistics.mean((x.get(k) or 0) for x in rs)
        calls = statistics.mean(((x.get("calls_ctx") or 0) + (x.get("calls_dx") or 0)) for x in rs)
        print(f"| {setting} | {cfg} | {len(rs)} | {calls:.1f} | {m('turns'):.1f} | "
              f"{m('mcp_result_tokens_est'):.0f} | {m('cost_usd'):.3f} | {m('wall_s'):.0f} |")

    print("\nPer-task means over repetitions: MCP calls (ctx+DX) / cost $ / wall s")
    per: dict[tuple, list[dict]] = collections.defaultdict(list)
    for r, _rep, _g in rows:
        per[(r["task"], r["setting"], r["config"])].append(r)
    for key in sorted(per, key=lambda k: (order[k[0]], k[1], k[2])):
        rs = per[key]
        calls = statistics.mean(((x.get("calls_ctx") or 0) + (x.get("calls_dx") or 0)) for x in rs)
        cost = statistics.mean((x.get("cost_usd") or 0) for x in rs)
        wall = statistics.mean((x.get("wall_s") or 0) for x in rs)
        print(f"{key[0]:4s} {key[1]:4s} {key[2]}  n={len(rs)}  calls={calls:4.1f}  "
              f"cost=${cost:.3f}  wall={wall:4.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
