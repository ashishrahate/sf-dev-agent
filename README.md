# Salesforce Developer Agent

An AI-powered CLI agent that plans, approves, and executes Salesforce development tasks against a real org — write Apex triggers, classes, flows, and validation rules from natural-language prompts.

The agent runs a **plan → approve → execute** loop:

1. **Phase 1 — Planning.** Inspects the org (read-only), drafts a structured plan with risk + rollback, submits it for approval.
2. **Approval gate.** You review the plan and type `yes` / `no` / `modify`.
3. **Phase 2 — Execution.** Only after approval does it write files and deploy to the org.

It is **provider-agnostic** — works with Anthropic Claude, OpenAI GPT, or Google Gemini.

---

## Quickstart (5 minutes)

```bash
git clone https://github.com/ashishrahate/sf-dev-agent
cd sf-dev-agent
uv sync && uv pip install -e '.[gemini]'    # pick your provider here
sf-agent doctor                              # verify system prereqs
sf-agent setup                               # interactive wizard
sf-agent "List all Apex classes in the org"  # one-shot task
sf-agent                                     # interactive REPL (no args)
```

That's the whole flow. After `uv pip install -e .` the `sf-agent` and `sfagent` binaries land on PATH — no `uv run` prefix needed.

---

## What you actually need to provide

| You provide | The agent figures out |
|---|---|
| ✅ One LLM API key (Gemini / OpenAI / Anthropic) | The provider, from whichever key you set |
| ✅ A Salesforce org alias | Instance URL, org type, API version |
|   | Workspace path (defaults to `<repo>/workspace`) |

The wizard prompts for the two on the left and writes a minimal `.env` for you.

---

## Prerequisites

| Requirement | Why | How |
|---|---|---|
| **Python 3.12+** | Runs the agent | [python.org/downloads](https://www.python.org/downloads/) |
| **uv** | Python package manager | `pip install uv` |
| **Salesforce CLI** | Talks to your org | `npm install -g @salesforce/cli` (Node 18+) |
| **An LLM API key** | The agent's brain | One of: Gemini (free), OpenAI, or Anthropic |

Verify all prereqs in one go:

```bash
sf-agent doctor
```

Probes Python / uv / Node / sf CLI / git and your LLM API key, and prints a color-coded table with the exact install command for any missing item on your OS. Run `sf-agent doctor --install` to print copy-paste install commands for everything that's red.

> **No Salesforce org yet?** Sign up free at [developer.salesforce.com/signup](https://developer.salesforce.com/signup) — pick "Developer Edition." Free forever, real org.

---

## The setup wizard

```bash
sf-agent setup
```

The wizard will:

1. Run `sf-agent doctor` first; refuses to proceed if any required tool is missing
2. Show your connected orgs in a numbered menu (or run `sf org login web` if you have none)
3. Let you pick an LLM provider — links you to the exact key page
4. Validate the key with a one-token test call (fail-fast on bad keys)
5. Write a minimal `.env`

After it finishes, you're done — go straight to "Running the agent."

---

## Running the agent

```bash
# One-shot
sf-agent "Create an Account trigger that prevents duplicate Phone numbers"

# Interactive REPL (Claude-Code-style — type once, then chat freely)
sf-agent

# Override the provider for a single run
sf-agent --provider openai "..."

# Test the loop without touching your org or burning LLM tokens
sf-agent --mock-org "Create a trigger"
```

When you ask for something that creates/modifies metadata, the agent:

1. Runs **preflight queries** (existing triggers, flows, validation rules on the target object).
2. Submits an **execution plan** with steps, risk level, rollback strategy, and impact counts.
3. Pauses at: `Approve this plan? [yes/no/modify]`
4. On `yes`, writes files to `workspace/force-app/main/default/` and runs `sf project deploy start` against your org with the test class.

**First run against a new org** — the agent will soft-prompt to warm the context engine (build the metadata index, embed components, embed the bundled knowledge base). It's a one-time ~30–90s setup; pick `skip` to defer or `no-and-stop-asking` to suppress it permanently for that org. The freshness state is pinned in every run's system prompt, so the agent self-detects when the index has gone stale and calls `build_metadata_index --delta` mid-task.

---

## Running interactively (the persistent REPL)

`sf-agent` with no arguments launches a persistent terminal session — type freely without a `sf-agent` prefix, hit Enter, and the agent runs. Slash commands manage state.

```text
$ sf-agent
╭─ Salesforce Developer Agent ─────────────────────╮
│ Org: my-prod (production) | API v64.0            │
│ Provider: GeminiProvider | Model: gemini-2.5-flash │
│                                                    │
│ Type freely. /help for commands. /quit to exit.  │
╰────────────────────────────────────────────────────╯
❯ what fields does Account have?
…
❯ /help
```

**Slash commands (12):** `/help` · `/status` · `/index` · `/tasks` · `/resume` · `/memory` · `/mock` · `/provider` · `/verbose` · `/clear` · `/quit` · `/exit`. Tab completion is wired up; history persists at `~/.sf-agent/history`. The bottom-line status bar shows the org, provider, in-flight task count, memory count, and index freshness at a glance.

**Streaming output.** Assistant tokens render live as they arrive. Press **ESC** (or **Ctrl+C**) at any time to interrupt mid-stream — the partial output is kept in the transcript so the next message has it as context. ESC during the LLM response also cancels any tool calls the model tried to emit.

**Resume by intent.** Just say what you mean: "what was I working on?" or "resume my last task". The agent calls `list_resumable_tasks` to browse working memory, optionally `get_task_summary` to confirm, then hands control back to the REPL via `request_resume`. The resumed task picks up from its last persisted state — same plan, same approval flag, same transcript — without you typing a second command.

**End-of-session memory extraction.** When you `/quit` after running tasks, the REPL soft-prompts: `Run end-of-session memory extraction now? [yes / skip / no-and-stop-asking]`. Yes walks each transcript through `MemoryExtractor` and lets you confirm each save-worthy candidate. `no-and-stop-asking` suppresses the prompt for that (tenant, org) pair permanently (delete the sentinel file under `.cache/` to re-enable).

`sfagent` (no hyphen) is an alias if you prefer fewer keystrokes — both forms launch the same REPL.

---

## Subcommand reference

| Command | What it does |
|---|---|
| `sf-agent` (no args) | Launch the interactive REPL. Type freely; no prefix needed. |
| `sf-agent "<request>"` | One-shot task. Process exits when the task ends — useful for scripts. |
| `sf-agent setup` | Interactive wizard: runs `doctor`, picks an org, picks a provider, writes `.env`. |
| `sf-agent doctor` | Probe system prereqs (Python / uv / Node / sf CLI / git / LLM key). `--install` prints copy-paste install commands. |
| `sf-agent resume <task-id>` | Pick up a persisted task that crashed, was paused, or is awaiting approval. |
| `sf-agent resume --list` | Show in-flight tasks (status / plan state / description / age). |
| `sf-agent resume --latest` | Resume the most-recent in-flight task. |
| `sf-agent memory extract --task-id <id>` | Scan a completed task's transcript for save-worthy moments — confirms each candidate before saving. |
| `sf-agent memory export [--type] [--out]` | Dump memories to Markdown for backup / git versioning. |
| `sf-agent memory promote --memory-id <id> --category <cat>` | Draft a knowledge-base entry from a project memory. Heuristic-blocked on tenant-specific content; `--force` to override. |

`sfagent` (no hyphen) is an alias for the same binary if you prefer fewer keystrokes.

---

## Context engine

The agent has a **hybrid context engine** (4 layers) that retrieves relevant code, schema, knowledge, and past memories for every task. Build it once per org and refresh after deploys:

```bash
# Inside the agent's REPL, or via natural-language request:
sf-agent "Build the metadata index for the current org"
sf-agent "Embed the metadata index for semantic search"
sf-agent "Embed the bundled knowledge base"
```

The agent invokes these tools (`build_metadata_index`, `embed_metadata_index`, `embed_knowledge_base`) automatically when it judges them necessary. They're idempotent — hash-gated re-embedding skips unchanged rows; `build_metadata_index --delta` re-fetches only what changed against the org's Tooling-API `LastModifiedDate`.

The four layers:

| Layer | What | How retrieved |
|---|---|---|
| Metadata index | Org schema, classes, triggers, dependency graph | `code_search`, `sf_dependency_graph` |
| Vector store | Embedded code chunks (Gemini `gemini-embedding-001`, 3072-d) | `semantic_search` |
| Knowledge base | 32 bundled SF best-practice / governor-limit / pattern entries | `knowledge_search` |
| Memory store | Past decisions, preferences, constraints from prior sessions | `memory_recall` |

For open-ended exploration, prefer `retrieve_context` — it fans out to all four layers in one call and returns a deduped, token-budgeted payload.

---

## Memory tier

The agent persists three kinds of state across sessions:

- **Project memory** — durable user / feedback / project / reference rows scoped per (tenant, org). Surfaced via `memory_recall` and the orchestrator. Save manually with `memory_save` or via end-of-session extraction (below).
- **Working memory** — every task's full conversation transcript and state machine, persisted to SQLite. Lets you resume an interrupted task without losing context.
- **Learning memory** — manually curated promotion path from project memory to the cross-tenant bundled knowledge base.

### Memory subcommand

```bash
# Scan a completed task's transcript for save-worthy moments
sf-agent memory extract --task-id task_20260427120000

# Dump memories to Markdown for git versioning / backup / sharing
sf-agent memory export [--type feedback] [--out ./memory-snapshots]

# Promote a project memory to a bundled knowledge entry
sf-agent memory promote \
    --memory-id local-dev:OrgA:bulkify-trigger:abcd1234 \
    --category best_practice
```

`memory extract` walks each LLM-proposed candidate (yes / no / edit) before persisting — no automatic writes. `memory promote` runs a tenant-specific-content heuristic and refuses to write the draft unless `--force`; `--force` still inlines the warnings into the file as a REVIEW comment so they're impossible to miss.

See `docs/PROJECT_SUMMARY.md` "Wave 8 shipped end-to-end" for the full memory architecture.

---

## Manual configuration (skip the wizard)

If you'd rather edit `.env` by hand, the **minimum** is:

```ini
GOOGLE_API_KEY=AIzaSy...your-key-here
SF_ORG_ALIAS=AgentforceOrg
```

That's it. The agent auto-detects the provider from the key, derives the org type and instance URL from the sf CLI, and reads the API version from `workspace/sfdx-project.json`.

See `.env.example` for the complete list of optional overrides.

---

## Command reference

```bash
# Provider / model
--provider {anthropic|openai|gemini}     # auto-detected from API key if unset
--model gemini-2.5-pro                   # override the provider's default

# Org overrides (rarely needed — auto-detected from sf CLI)
--org-alias MyScratch
--org-type {sandbox|scratch|production|developer}
--instance-url https://test.salesforce.com
--api-version 62.0

# Misc
--mock-org                               # canned SF responses; LLM still real
--verbose                                # debug logging
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `No LLM provider configured` | Set one of `GOOGLE_API_KEY` / `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` in `.env`, or run the wizard |
| `Multiple API keys are set` | Set `LLM_PROVIDER=...` in `.env` to pick one, or pass `--provider` |
| `No Salesforce org configured` | Set `SF_ORG_ALIAS=...` in `.env` (must match a name from `sf org list`) |
| `FileNotFoundError: [WinError 2]` calling `sf` | Run `npm install -g @salesforce/cli`; agent uses `sf.cmd` on Windows |
| `429 RESOURCE_EXHAUSTED` (Gemini) | Daily free-tier quota hit. Create a new Google AI Studio project, enable billing, or wait until midnight Pacific. Note: rotating keys in the *same* project shares the same quota. |
| `InvalidProjectWorkspaceError` from sf | `workspace/sfdx-project.json` is missing or invalid. The repo ships with one — verify it exists. |
| `NotADevHubError` | Your target org isn't a Dev Hub. Either point at a Developer Edition org directly or enable Dev Hub in your org's Setup. |
| Plan never appears, agent just answers | Read-only question → no plan needed. For write ops, phrase it as create/modify. |
| `Provider not installed` | `uv pip install -e '.[gemini]'` (or `[openai]` / `[anthropic]` / `[all]`) |

---

## Project layout

```
sf-dev-agent/
├── src/sf_dev_agent/
│   ├── __main__.py             # CLI entry point + setup / memory dispatchers
│   ├── setup_wizard.py         # interactive setup flow
│   ├── agent.py                # ReAct loop, plan-approve-execute, resume()
│   ├── paths.py                # repo_root() / agent_workspace() helpers
│   ├── sf_config.py            # auto-derive org type / instance URL / API version
│   ├── memory_cli.py           # `sf-agent memory <verb>` dispatch
│   ├── providers/              # anthropic / openai / gemini adapters
│   ├── tools/registry.py       # sf CLI wrappers, file I/O, bash, context, memory
│   ├── prompts/                # system prompt template
│   ├── models/schemas.py       # Pydantic models (Task, ExecutionPlan, ...)
│   ├── context/                # hybrid context engine — index, vectors, knowledge, orchestrator
│   │   ├── index.py            # SQLite metadata index
│   │   ├── orchestrator.py     # retrieve_context — 4-layer fan-out
│   │   ├── delta.py            # Tooling-API LastModifiedDate diff
│   │   ├── parsers/            # ApexClass / ApexTrigger / CustomObject parsers
│   │   ├── embedders/          # Gemini + mock embedder adapters
│   │   ├── knowledge/          # bundled best-practice / governor-limit entries
│   │   └── schema.sql          # all SQLite tables (index + memory + working memory)
│   └── memory/                 # working / project / learning memory tiers
│       ├── store.py            # MemoryStore — project memory
│       ├── working.py          # WorkingMemoryStore — task state + transcript
│       ├── conversation_log.py # list-shaped wrapper that mirrors append() to disk
│       ├── extraction.py       # MemoryExtractor — LLM-driven end-of-session capture
│       ├── export.py           # MemoryExporter — Markdown round-trip
│       └── promote.py          # MemoryPromoter — drafts knowledge entries
├── workspace/                  # SFDX project — agent reads/writes metadata here
├── tests/                      # pytest suite (231 default tests)
├── docs/                       # design notes, project summary, roadmap, session logs
│   ├── PROJECT_SUMMARY.md      # architecture + as-shipped state
│   ├── ROADMAP.md              # backlog by plane + UX deliverables
│   └── sessions/               # per-session design logs
└── .env                        # your config (gitignored)
```

---

## Safety model

- **Read-only tools** (`sf_metadata_describe`, `sf_soql_query`, `sf_retrieve`, `code_search`, `file_read`) run freely during planning.
- **Write tools** (`file_write`, `sf_source_deploy`, `sf_apex_execute`, `bash`) are **blocked during Phase 1** — they fail with a clear error if the LLM tries to call them before approval.
- **Approval is required** before any write can execute. There is no `--yes` flag.
- **Mock-org mode** (`--mock-org`) stubs all `sf` CLI calls but the LLM is still real — useful for testing prompts and the plan flow without touching your org.

---

## Roadmap

The hybrid context engine, retrieval orchestrator, and memory tiers (working / project / learning) are all shipping. **Wave 8 closed the memory architecture** as of 2026-04-27 — see `docs/PROJECT_SUMMARY.md` "Wave 8 shipped end-to-end" for the full picture.

The current backlog lives in [`docs/ROADMAP.md`](docs/ROADMAP.md). It's organized in two parts:

- **Part 1 — architecture gaps by plane**: agent (~95%), data (~70%), control (~20%), execution (~5%). Lists every item from the PROJECT_SUMMARY architecture description that isn't yet built, with priority and effort.
- **Part 2 — UX deliverables**: `sf-agent doctor` (system prereq check), `sf-agent resume` (CLI verb for crash recovery), auto-warm context engine + staleness check, end-of-session extract nudge, persistent terminal REPL with slash commands. Phased so each item is independently shippable.

Per-session design logs are in `docs/sessions/`. The day Wave 8 shipped is captured in `docs/sessions/2026-04-27.md`.
