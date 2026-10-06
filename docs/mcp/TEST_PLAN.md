# Cross-agent test plan

Question: does sf-context make coding agents better at Salesforce work, alone and next to Salesforce's DX MCP server, and is it worth its context cost?

## Setup (once)

1. `uv run python scripts/mcp_preflight.py --org AgentforceOrg` shows nothing MISSING.
2. Use a **sandbox/dev org** only. DX MCP can deploy; run write tasks (T2) against a throwaway org.
3. Freeze the org state between runs (no manual edits). Build the index once and note the commit/time.
4. Fix the model per agent for all configs; record it in RESULTS.md.

## Configurations

| | sf-context | Salesforce DX MCP | Purpose |
|---|---|---|---|
| **A** | off | on | baseline: what agents do today |
| **B** | on | off | what context adds without any action tools |
| **C** | on | on | the intended combination |

Agents to cover (at least two): Claude Code, plus one of Cursor / VS Code Copilot. Same prompt text for every run; fresh session for each (except T4).

## Task battery

Fill the **answer key** column from the index/org *before* running (`dependency_graph` on the named component gives most of it). Objects below come from the AgentforceOrg pressure-test queries; swap for anything that exists in the org you use.

| # | Prompt (verbatim) | Needs | Answer key (fill first) |
|---|---|---|---|
| T1 | "List everything in this org that touches the Account object: triggers, flows, validation rules and the classes involved, and how they depend on each other." | graph + index | |
| T2 | "Add validation to AccountTriggerHandler so it rejects an Account with negative AnnualRevenue. Don't break existing callers, follow this org's trigger conventions, include a test class, and deploy it to the org." | graph + code + best practice + deploy | |
| T3 | "Review AccountTriggerHandler against Salesforce best practices. Which governor-limit or anti-pattern problems apply?" | code + knowledge | |
| T4a | "Record this decision for the team: we prefer Apex triggers with a handler class over Flows for complex Account logic, because the team owns the Apex skills." (end session) | memory write | saved with Why/How |
| T4b | *(new session)* "I need to add logic on Account insert. How should I do it here?" | memory recall | mentions the T4a decision unprompted |
| T5 | "Which classes call RecordSelectorController, and what would break if I rename it?" | graph (incoming) | |
| T6 | After editing a class in the org: "Is your view of the org up to date? Refresh it if not, then tell me what changed." | staleness + delta refresh | |

## What to record per run

Copy a row into `RESULTS.md` for each (task, agent, config):

| Field | How |
|---|---|
| Correctness (0-3) | 0 wrong, 1 partial, 2 correct, 3 correct and noticed something the key missed. Grade against the key. |
| Tool calls | count, and which ones (sf-context vs DX) |
| Wrong/hallucinated claims | count of statements contradicted by the org |
| Tokens | input + output tokens for the whole task if the client shows them; otherwise note "n/a" |
| Wall-clock | seconds, prompt to final answer |
| Used sf-context unprompted? | y/n (does the agent reach for it from the tool descriptions alone?) |
| Notes | failures, wrong tool choice, timeouts, mock-embedder warnings |

## Decision criteria

- **Adopt** if C beats A on correctness for T1/T2/T5 with fewer tool calls or fewer wrong claims, and T4b recalls the decision.
- **Fix descriptions** if agents don't call sf-context unprompted (B/C) or pick the wrong tool.
- **Trim the tool set** if tool-definition overhead (about 2,100 tokens) outweighs gains, or agents get confused between the two servers' tools.
- **Investigate embedder** if semantic results look poor: compare `fastembed` with `SF_CONTEXT_EMBEDDER=gemini` on T1, T3 and T4b.

## Known limits of this test

Small sample, non-deterministic models: repeat each cell 2-3 times and report the range. Client-side token counts are not available in every agent; don't compare tokens across different clients.
