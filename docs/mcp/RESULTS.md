# Cross-agent test results

**Run date:** 2026-10-06. **Agent:** headless Claude Code 2.1.291 (`claude -p`, model alias `sonnet`), one agent only. Cursor and VS Code Copilot are interactive and were **not** tested. **One run per cell**, no repeats. Runner: `scripts/mcp_bench.py`; per-run summaries in `docs/mcp/runs/`.

| Item | Value |
|---|---|
| Org | `AgentforceOrg` (developer edition; 27 Apex classes, 3 triggers, 3 custom objects, 2 flows, 1 LWC; 0 validation rules) |
| Index | 47 components / 43 relationships, 100% embedded with `fastembed:BAAI/bge-small-en-v1.5` (384-d, no API key) |
| sf-context | commits on `feature/context-mcp-server`; 12 tools, about 2,100 tokens of definitions |
| Salesforce DX MCP | `@salesforce/mcp` v0.30.15, toolsets `orgs,data,metadata,testing`: 8 tools, about 4,000 tokens of definitions |

**Configs:** A = DX MCP only, B = sf-context only, C = both. **Settings:** `repo` = working dir is a retrieved DX project and the agent has Read/Write/Edit/Glob/Grep; `org` = empty dir and no file tools, so the agent can learn about the org only through MCP. Each run used an isolated copy of the index DB.

## Headline

- **Correctness did not separate the configs.** Every T1/T3/T5 cell scored full marks on a keyword rubric (see limits below), so this test cannot show that sf-context makes answers *better*.
- **It does make the agent cheaper and faster when it has no local files.** In the `org` setting, across T1, T3 and T5: DX alone needed 25 MCP calls and returned about 18,200 tokens of tool results at $0.33 and 107 s; sf-context alone needed 13 calls, about 8,200 tokens, $0.18, 56 s. Both together landed in between (15 calls, 8,600 tokens, $0.22, 78 s). The biggest gap is T1: 13 DX calls against 7 for sf-context.
- **With a local repo the advantage mostly disappears.** `repo` setting totals: A $0.21 / 83 s, B $0.16 / 46 s, C $0.20 / 70 s. The agent can simply read files.
- **In config C the agent chose sf-context for read-only questions and DX only to deploy** (T2: 3 DX calls, 2 sf-context calls).
- **Cross-session memory worked:** after T4a saved the team's "triggers over Flows" decision, a new session's T4b recalled it and cited it, in both settings and both configs B/C (A has no persistent store).
- **Tool-definition overhead is real:** about 2,100 tokens for sf-context, about 4,000 for the four DX toolsets, paid on every turn.

## Results

Correctness is hits / required terms on the rubric in `scripts/mcp_bench.py` (T2: heuristic checks on the produced code). Tokens are the estimated size of MCP tool results.

| Task | Setting | Cfg | Rubric | MCP calls ctx/DX | Other tools | Turns | MCP result tok | Cost | Wall |
|---|---|---|---|---|---|---|---|---|---|
| T1 | org | A | 6/6 | 0/13 | 0 | 14 | 11,503 | $0.182 | 45 s |
| T1 | org | B | 6/6 | 7/0 | 0 | 8 | 1,729 | $0.065 | 15 s |
| T1 | org | C | 6/6 | 8/0 | 0 | 9 | 1,950 | $0.097 | 25 s |
| T3 | org | A | 3/3 | 0/5 | 0 | 6 | 5,946 | $0.089 | 32 s |
| T3 | org | B | 3/3 | 4/0 | 0 | 5 | 5,879 | $0.089 | 31 s |
| T3 | org | C | 3/3 | 4/0 | 0 | 5 | 6,002 | $0.095 | 35 s |
| T5 | org | A | 3/3 | 0/7 | 0 | 8 | 756 | $0.059 | 31 s |
| T5 | org | B | 3/3 | 2/0 | 0 | 3 | 581 | $0.027 | 10 s |
| T5 | org | C | 3/3 | 3/0 | 0 | 4 | 677 | $0.030 | 18 s |
| T1 | repo | A | 6/6 | 0/0 | 10 | 11 | 0 | $0.116 | 27 s |
| T1 | repo | B | 6/6 | 7/0 | 0 | 8 | 1,728 | $0.048 | 15 s |
| T1 | repo | C | 6/6 | 6/0 | 0 | 7 | 1,667 | $0.103 | 22 s |
| T3 | repo | A | 3/3 | 0/0 | 6 | 7 | 0 | $0.067 | 38 s |
| T3 | repo | B | 3/3 | 2/0 | 2 | 5 | 4,845 | $0.075 | 20 s |
| T3 | repo | C | 3/3 | 2/0 | 7 | 10 | 382 | $0.067 | 29 s |
| T5 | repo | A | 3/3 | 0/0 | 1 | 2 | 0 | $0.026 | 18 s |
| T5 | repo | B | 3/3 | 2/0 | 1 | 4 | 581 | $0.033 | 11 s |
| T5 | repo | C | 3/3 | 1/0 | 1 | 3 | 239 | $0.032 | 20 s |
| T2 | repo | A | 6/6 | 0/3 | 12 | 16 | 1,637 | $0.127 | 42 s |
| T2 | repo | B | 6/6 | 1/0 | 12 | 14 | 3,985 | $0.136 | 35 s |
| T2 | repo | C | 6/6 | 2/3 | 10 | 16 | 1,985 | $0.127 | 50 s |
| T4a | org | B / C | 1/1 | 1/0 | 0 | 2 | ~80 | $0.019 / $0.025 | 8 / 21 s |
| T4b | org | B / C | 2/2 | 3/0 | 0 | 4 | ~4,300 | $0.054 / $0.057 | 14 / 21 s |
| T4a | repo | B / C | 1/1 | 1/0 | 0 | 2 | ~90 | $0.020 / $0.025 | 10 / 21 s |
| T4b | repo | B / C | 2/2 | 2/0, 1/0 | 1, 2 | 4 | ~4,000 | $0.054 / $0.056 | 14 / 24 s |
| T6 | repo | B | 1/2 | 3/0 | 0 | 4 | 464 | $0.055 | 139 s |
| T6 | repo | C | 1/2 | 4/0 | 0 | 5 | 557 | $0.085 | 68 s |

T2 (add negative-AnnualRevenue validation, tests, deploy): all three configs edited `AccountService`/`AccountTriggerHandler`, did not add a second trigger and added tests (B created a new `AccountAnnualRevenue_Test` class; A and C edited `AccountTriggerHandler_Test`). A and C deployed through DX. B has no deploy tool, so it produced code only. T2 was **not** inspected line-by-line for correctness beyond the heuristic checks, and the deploys were **not** checked for test pass/fail.

## Defects found by running it (sf-context)

1. **Wrong flow status (accuracy).** All four sf-context runs of T1 said `Append_Billing_Country_to_Account_Name` is "Draft, so it isn't running". The org has an **Active v8 and a newer Draft v9**. The index stores the latest version's status. DX's live query got it right; so did none of the local-file runs (the retrieved project also holds the latest version). Fix: index the active version's status, or report both.
2. **Staleness is age-only.** `index_status` said "not stale" an hour after the index was built, right after the org had been edited, so the first T6 attempt never refreshed. Mitigated in this branch: the tool text and output now say staleness is age-based and recommend running the incremental `build_metadata_index`; on the rerun both B and C refreshed on their own.
3. **Refresh result doesn't name what changed.** T6 asks "what changed?". The build result has counts only, so neither agent could name `AccountSelector`. Needs a changed-component list in the result.
4. **Incremental refresh appears to drop custom objects (likely cause, not verified).** After the T6 refresh the index went from 47 to 36 components with "2 deleted". The delta inventory in `delta.py` builds every custom-object id as `<DeveloperName>__c`, so platform events (`__e`) and custom metadata types (`__mdt`) don't match their stored ids and would be treated as deleted, taking their fields with them (2 objects + 9 fields = 11). This matches the numbers but I did not confirm it in code or fix it.
5. **Fixed during the run:**
   - The server's `sf` subprocesses inherited the MCP server's stdin and hung forever on index build; fixed with `stdin=DEVNULL` (a build over stdio now takes about 1 minute instead of never finishing).
   - When the environment forces ANSI colour, `sf --json` output was unparseable, so refresh failed with "non-JSON output"; fixed with `NO_COLOR` plus escape stripping.
   - Memory saved with `memory_save` was never embedded, so recall could not find it; now embedded on save.

## Limits of this test: read before trusting the numbers

- **One agent, one model, one run per cell.** No repeat runs, no variance estimate. Differences of a few cents or seconds are noise.
- **The rubric is lenient.** It checks that expected names appear, not that nothing wrong was said. It scored the flow-status error as a pass. Only a few answers were read in full (T1 A/B/C, T4b, T5 A, T6).
- **Tiny org.** 27 classes is too small to show context savings at scale; the `repo` setting in particular gives file-reading agents an easy job.
- **Settings are artificial.** In `org` there are no file tools at all; in `repo` the agent has a full local copy. Real developers are somewhere in between.
- **Costs and timings include prompt caching** and one-off MCP server start-up; they are not a billing forecast.
- **T6 first attempts were invalid** (harness bugs, a hung server) and are kept as `T6_*_repo_attempt1.json`; use the `_r2` files. Both `_r2` runs still scored 1/2 because the agent could not name the changed class.
- **T2 deployed to the dev org** in configs A and C. The first restore failed (harness bug); the org was redeployed from the baseline afterwards and every class and trigger body was verified identical to the original.
- `AgentforceOrg` state is the baseline retrieved at test time; runs happened in a single sitting.

## Verdict

Evidence is thin but points one way: sf-context is a **cost and speed win when the agent has no local source** (about half the tool calls and tool-result tokens for read-only questions) and **neutral when it does**. It did not improve measured correctness, and it introduced one wrong-but-plausible claim (flow status) that DX's live query avoided. Memory is the one capability DX lacks and it worked.

## Follow-ups

- Fix defects 1, 3 and 4 above, then rerun T1 and T6.
- Repeat the matrix 3x for variance; add a larger org so context savings can show.
- Run Cursor and Copilot by hand using `configs/` and the prompts in `TEST_PLAN.md` (cannot be scripted from here).
- Tighten the rubric: add forbidden-claim checks and grade a sample of answers by reading them.
- Compare `fastembed` with `SF_CONTEXT_EMBEDDER=gemini` on T3 and T4b (not done).
