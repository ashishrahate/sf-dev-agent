# Cross-agent test results

**Run date:** 2026-10-06. **Agent:** headless Claude Code 2.1.291 (`claude -p`, model alias `sonnet`). One agent only: Cursor and VS Code Copilot are interactive and were **not** run (procedure in `MANUAL_TESTING.md`). **Three repetitions per cell.** Runner: `scripts/mcp_bench.py`; regrading and tables: `scripts/mcp_regrade.py`; per-run summaries in `docs/mcp/runs/`.

| Item | Value |
|---|---|
| Org | `AgentforceOrg` (developer edition; 27 Apex classes, 3 triggers, 3 custom objects, 2 flows, 1 LWC; 0 validation rules) |
| Index | 47 components / 43 relationships, 100% embedded with `fastembed:BAAI/bge-small-en-v1.5` (384-d, no API key) |
| sf-context | branch `feature/context-mcp-server`; 12 tools, about 2,100 tokens of definitions |
| Salesforce DX MCP | `@salesforce/mcp` v0.30.15, toolsets `orgs,data,metadata,testing`: 8 tools, about 4,000 tokens of definitions |

**Configs:** A = DX MCP only, B = sf-context only, C = both. **Settings:** `repo` = working dir is a retrieved DX project and the agent has Read/Write/Edit/Glob/Grep; `org` = empty dir, no file tools, so the agent can learn about the org only through MCP. Each run used an isolated copy of the index DB.

**Repetitions:** repetition 1 was run **before** the defect fixes below; repetitions 2 and 3 **after**. The grids mark pre-fix wrong claims, so the effect of the fixes is visible.

## Headline

1. **Without local source, sf-context makes the agent roughly 2 to 3 times cheaper and faster than DX alone.** `org` setting, read-only tasks (T1, T3, T5), mean per task over 3 repetitions (n = 9 runs per row):

   | Config | MCP calls | Tool-result tokens | Cost | Wall time |
   |---|---|---|---|---|
   | A (DX only) | 10.4 | 13,336 | $0.172 | 42 s |
   | B (sf-context only) | 4.8 | 2,875 | $0.059 | 17 s |
   | C (both) | 5.1 | 3,230 | $0.071 | 23 s |

   About half the tool calls, about a fifth of the tool-result tokens, about a third of the cost and 40% of the time. The gap is widest on T1 (15.3 DX calls against 8.3) and T5 (10.3 against 2.0).
2. **With a local repo the advantage mostly disappears.** `repo` setting: A $0.066 / 28 s, B $0.053 / 17 s, C $0.060 / 23 s. The agent just reads files (config A used no DX tools at all for read-only tasks).
3. **Correctness did not separate the configs** on T2, T3, T4, T5: every cell passed in all repetitions. The one place correctness differed was **T1's flow status**, below.
4. **A wrong claim that sf-context introduced was found and fixed.** In repetition 1, all four sf-context T1 runs (B and C, both settings) said the Account flow is "Draft, so it isn't running" (it has an Active v8 and a newer Draft v9; the index stored the draft). After the fix, all 8 sf-context T1 runs in repetitions 2 and 3 are right. The same error persists in **config A with local files** (DX plus the retrieved draft source) in all 3 repetitions, and is absent from config A without local files (live queries).
5. **Cross-session memory works:** T4b recalled and cited the saved decision unprompted in 12 of 12 runs (both settings, B and C, 3 repetitions).
6. **Refreshing the index now works end to end (T6).** After the fixes, both B and C refresh on their own and name `ApexClass:AccountSelector` as the only modified component, with all 47 components retained, in 4 of 4 runs. Before the fixes: 0 of 2 (first attempts hung or failed, and the rerun could not name the change and shrank the index to 36).
7. **In config C the agent chose sf-context for read-only questions and DX only to deploy** (T2: 3 DX calls, all for deploy).
8. **Tool-definition overhead is real:** about 2,100 tokens for sf-context, about 4,000 for the four DX toolsets, paid every turn.

## Correctness by cell

Required terms hit / total. `*` = the run made a claim on the wrong-claim list (T1: "the flow is not running"). T6 rep 1 is the post-hang rerun, before the delta fixes.

| Task | Setting | Cfg | Rep 1 (pre-fix) | Rep 2 | Rep 3 |
|---|---|---|---|---|---|
| T1 | org | A | 6/6 | 6/6 | 6/6 |
| T1 | org | B | 6/6* | 6/6 | 6/6 |
| T1 | org | C | 6/6* | 6/6 | 6/6 |
| T1 | repo | A | 6/6* | 6/6* | 6/6* |
| T1 | repo | B | 6/6* | 6/6 | 6/6 |
| T1 | repo | C | 6/6* | 6/6 | 6/6 |
| T2 | repo | A / B / C | 6/6 | 6/6 | 6/6 |
| T3 | org, repo | A / B / C | 3/3 | 3/3 | 3/3 |
| T4a | org, repo | B / C | 1/1 | 1/1 | 1/1 |
| T4b | org, repo | B / C | 2/2 | 2/2 | 2/2 |
| T5 | org, repo | A / B / C | 3/3 | 3/3 | 3/3 |
| T6 | repo | B | 1/2* | 2/2 | 2/2 |
| T6 | repo | C | 1/2* | 2/2 | 2/2 |

Per-task means (calls = sf-context + DX calls; cost and wall are per run, mean of 3):

| Task | Setting | A: calls / $ / s | B: calls / $ / s | C: calls / $ / s |
|---|---|---|---|---|
| T1 | org | 15.3 / 0.272 / 50 | 8.3 / 0.069 / 18 | 8.3 / 0.095 / 24 |
| T1 | repo | 0 / 0.108 / 26 | 7.0 / 0.050 / 17 | 6.7 / 0.078 / 23 |
| T3 | org | 5.7 / 0.092 / 39 | 4.0 / 0.083 / 25 | 4.0 / 0.088 / 30 |
| T3 | repo | 0 / 0.065 / 40 | 2.0 / 0.077 / 22 | 1.3 / 0.070 / 27 |
| T5 | org | 10.3 / 0.151 / 38 | 2.0 / 0.026 / 9 | 3.0 / 0.031 / 16 |
| T5 | repo | 0 / 0.025 / 16 | 2.0 / 0.033 / 11 | 1.0 / 0.032 / 18 |
| T2 | repo | 3.0 / 0.132 / 42 | 1.7 / 0.127 / 28 | 4.7 / 0.148 / 47 |
| T4a | org / repo | n/a | 1.0 / 0.019 / 8 and 1.0 / 0.022 / 10 | 1.0 / 0.024 / 18 and 1.0 / 0.025 / 17 |
| T4b | org / repo | n/a | 3.3 / 0.053 / 16 and 1.3 / 0.054 / 13 | 3.0 / 0.057 / 19 and 1.3 / 0.058 / 20 |
| T6 | repo | n/a | 3.0 / 0.036 / 135 | 3.3 / 0.050 / 67 |

Notes on individual tasks:

- **T2 (add negative-AnnualRevenue validation, tests, deploy):** all three configs, all three repetitions, passed the code checks: logic added through the existing trigger, handler and service chain, no second trigger, tests added, no query or DML in a loop (heuristic). A and C deployed through DX (verified by a `deploy_metadata` call); **B has no deploy tool and said so honestly in every repetition** instead of claiming a deployment. In two runs (A and C, rep 3) the agent created a new test class in the org, which the harness deleted. T2 code was graded by pattern checks, **not compiled and not test-run**, so "passes" means "looks right", not "runs".
- **T6 duration:** 67 to 135 s per run. The refresh is the cost: Flow, LWC, ValidationRule and RecordType are always fully re-fetched even in incremental mode (see follow-ups).

## Defects found and their status

| # | Defect | Status |
|---|---|---|
| 1 | **Wrong flow status.** Index stored the latest version's status (Draft v9) for a flow whose v8 is Active and running; agents repeated "not running". | **Fixed and verified.** Each indexed flow's metadata now carries `is_active`, `active_version`, `latest_version` and a note explaining the draft. 8 of 8 post-fix sf-context T1 runs correct. |
| 2 | **Staleness is age-only.** `index_status` said "not stale" right after an org edit. | **Mitigated.** Output and tool descriptions now say so and recommend the incremental `build_metadata_index`; agents now do it unprompted (T6, 4 of 4). A real change check (compare against org timestamps) is still not in `index_status`. |
| 3 | **Refresh result did not say what changed.** | **Fixed and verified.** `build_metadata_index` returns `changes: {added, modified, removed, counts}` from a before/after content comparison. |
| 4 | **Incremental refresh dropped custom objects** (47 to 36 components): Tooling `CustomObject` omits the suffix, and the code assumed `__c`, so platform events (`__e`) and custom metadata types (`__mdt`) looked deleted and took their fields with them. | **Confirmed in code and fixed.** The true suffix now comes from `EntityDefinition`; falls back to `__c` if that lookup fails. Verified live: full rebuild then incremental refresh keeps all 47 components with 0 deleted. |
| 5 | `sf` subprocesses inherited the MCP server's stdin and hung forever on index build. | **Fixed** (`stdin=DEVNULL`); build over stdio now takes about 1 minute. |
| 6 | When the environment forces ANSI colour, `sf --json` was unparseable and refresh failed ("non-JSON output"). | **Fixed** (`NO_COLOR` plus escape stripping). |
| 7 | Memory saved with `memory_save` was never embedded, so recall could not find it. | **Fixed** (embedded on save). |

Regression tests: `packages/sf-context-engine/tests/test_index_fixes.py` and `test_sfcli.py`.

## Limits of this test: read before trusting the numbers

- **One agent, one model.** Cursor and Copilot are untested. Other models may pick tools differently.
- **n = 3 per cell.** Enough to see consistent patterns (T1 flow claim, call counts), not enough for fine cost or timing differences. Costs include prompt caching and MCP server start-up; they are not a billing forecast.
- **Tiny org.** 27 classes is too small to show how context savings scale. The `repo` setting in particular is an easy job for a file-reading agent.
- **The rubric is still keyword-based.** It now includes a forbidden-claim check for the known flow trap, but an agent could still say something else wrong that no pattern catches. Only a handful of answers were read in full. T2 is pattern-checked, not compiled or run.
- **The rubric was corrected during the run.** T2 and T6 checks were fixed after repetition 2 (a pattern that missed correct code, a deploy check that matched wording instead of behaviour, a T6 pattern that missed "ran an incremental build"). All stored runs were regraded with the final rubrics (`mcp_regrade.py`).
- **Settings are artificial.** `org` has no file tools at all; `repo` has a full local copy. Real developers are in between.
- **Org writes.** T2 deployed to the dev org in configs A and C. After every repetition the org was redeployed from the baseline and, after the last, every class and trigger body was compared with the original: **identical**. An earlier restore step failed because of a harness bug (fixed); the agents' edits were live in the org for a while during the first run.
- **Embedder comparison not done.** All runs used `fastembed`; `gemini` was not compared.

## Verdict

- **Adopt it for agents that lack local source.** It cut tool calls roughly in half, cost to about a third and time to about 40% against DX alone, with equal correctness on everything except the flow trap, which sf-context itself introduced and is now fixed.
- **Where the agent has the repo, it is a small win at best** (about 10 to 20% cheaper and 20 to 40% faster), plus capabilities files can't give: dependency edges, saved decisions and best-practice lookup.
- **Memory is the standout capability DX lacks,** and it worked in every run.
- **Keep DX MCP alongside it for actions.** Config C used DX only to deploy, which is the intended split. B cannot deploy and said so.
- **Open concerns:** staleness cannot be detected without an index refresh (about 1 to 2 minutes), the 2,100-token tool overhead, and results from a small org and a single agent.

## Follow-ups

- Run Cursor and Copilot by hand with `MANUAL_TESTING.md`; add their rows here.
- Make incremental refresh genuinely incremental for Flow, LWC, ValidationRule and RecordType (delta covers only ApexClass, ApexTrigger and CustomObject today), and add a cheap "has the org changed?" check to `index_status`.
- Try a bigger org and compare `fastembed` against `gemini` (`SF_CONTEXT_EMBEDDER=gemini`) on T3 and T4b.
- Compile and run the T2 output in a scratch org instead of pattern-checking it.
