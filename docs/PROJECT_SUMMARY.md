# Salesforce Developer Agent — Project Summary

## Vision

Replace a human Salesforce developer with an autonomous AI agent that can write Apex, build LWCs, configure metadata, deploy to orgs, debug issues, and manage the full Salesforce development lifecycle — securely, against real client orgs, at production scale.

---

## Architecture Overview

The system is composed of four planes:

### Control Plane
The orchestration layer. Manages authentication, tenant lifecycle, task state machines (plan → approve → execute), audit logging, and billing. This is the "platform" that makes multi-tenant operation safe.

### Agent Plane
The intelligence layer. A ReAct-style agent loop powered by Claude, equipped with Salesforce-specific tools. The agent reasons about tasks, queries org metadata, writes code, and executes deployments. The agent operates in two phases:

- **Phase 1 — Planning (autonomous, read-only):** The agent analyzes the request, explores the org, gathers context from the metadata index / vector store / knowledge base, and produces a structured execution plan with risk assessment and rollback strategy.
- **Phase 2 — Execution (gated, requires approval):** After user approval, the agent executes the plan step by step, writing code, deploying, running tests, and self-correcting on failures.

### Execution Plane
Ephemeral sandboxed containers (Firecracker / gVisor) where code actually runs. Each session gets a fresh container with sfdx CLI, Node.js, PMD, and the agent's tool wrappers. Network egress is locked to the client's Salesforce instance and the control plane. Credentials exist only in-memory and are destroyed when the container terminates.

### Data Plane
The client's Salesforce org(s). We never own client data — we access it through scoped, revocable OAuth 2.0 tokens with least-privilege permissions. The client's Salesforce admin controls what the Connected App can access.

---

## Hybrid Context Engine (Three Layers)

The agent's ability to find the *right* context from a large org is what separates a demo from a product.

### Layer 1 — Code Vector Store (Semantic Search)
Every Apex class, trigger, LWC, and Aura component in the client's org is chunked by semantic unit (class signatures, individual methods, test methods, wire adapters) and embedded into a vector database. The agent queries this with natural language to find relevant existing code.

**Storage:** pgvector / Pinecone / Qdrant (tenant-partitioned)
**Update cadence:** On every sync and post-deploy

### Layer 2 — Org Metadata Index (Structured / Graph)
A relational + graph index of the org's metadata: object schemas, field relationships, trigger inventories, class dependency graphs, flow inventories, permission sets. This answers "what exists" and "what depends on what" — not a vector search problem, but a graph traversal problem.

**Storage:** PostgreSQL + graph capabilities (pg_graphql or Neo4j)
**Update cadence:** Full sync nightly, incremental sync on every deploy

### Layer 3 — Knowledge Base (Domain Expertise)
Curated Salesforce best practices, governor limit reference, design patterns (trigger frameworks, selector/domain/service layers), common error resolutions, and release notes. Shared across all tenants.

**Storage:** Document store with keyword + vector search
**Update cadence:** Manual curation, weekly refresh

### Retrieval Orchestrator
For every task, the orchestrator queries all three layers in parallel and assembles a focused context window (~3,000–5,000 tokens of highly relevant context) from the results. This replaces the "cat every file" approach that works locally but fails at org scale.

---

## Stateful Memory (Three Tiers)

### Working Memory (session-scoped)
The conversation history, current plan, intermediate tool results. Lives in Redis, keyed by task_id. Destroyed or archived on task completion.

### Project Memory (cross-session, tenant-scoped)
Extracted decisions, preferences, warnings, and context from completed tasks. Persisted in the database, retrieved by the context engine at the start of each new task. Example: "Client prefers trigger-based approach over Flows for complex logic."

### Learning Memory (global, curated)
Patterns learned across all clients that apply universally. Reviewed and promoted manually into the Knowledge Base (Layer 3).

---

## Security Model

- **OAuth 2.0 Web Server flow** for org connections. Refresh tokens encrypted at rest (AES-256) in a per-tenant secrets vault.
- **Container-per-session isolation.** No shared filesystem, no cross-tenant access.
- **Network egress restricted** to the client's specific Salesforce instance URL and the control plane.
- **LLM calls via Anthropic API** with zero-data-retention configuration.
- **Three-tier permission enforcement:** platform level (subscription), org level (OAuth scopes), task level (plan approval gates).
- **Audit trail** on every tool invocation with tenant_id, task_id, tool_name, parameters (scrubbed of PII), outcome, and duration.

---

## Human-in-the-Loop Model

The agent uses a **plan → approve → execute** pattern:

- **Read-only operations** (metadata queries, SOQL, code retrieval, static analysis) execute autonomously during planning.
- **Write operations** (code creation, deployments, data modifications) require explicit user approval of the plan.
- **Destructive operations** (metadata deletion, production changes) require approval AND a rollback strategy.
- Gate granularity is configurable per client: some want hard gates on everything, power users may loosen to soft gates for sandbox operations.

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Agent LLM | Claude (Anthropic API, claude-sonnet-4) |
| Agent SDK | Anthropic Claude Agent SDK (Python) |
| Agent loop | Custom ReAct implementation |
| CLI / local tools | Salesforce CLI (sf v2), Node.js, PMD |
| Vector store | pgvector (MVP) → Pinecone/Qdrant (scale) |
| Metadata index | PostgreSQL |
| Memory store | Redis (working) + PostgreSQL (project) |
| Container runtime | Docker (MVP) → Firecracker/gVisor (prod) |
| Auth / secrets | python-dotenv (MVP) → AWS Secrets Manager / Vault (prod) |
| Orchestration | Python async (MVP) → Kubernetes jobs (prod) |

---

## Current Phase: Memory tiers (Week 9-10) — planned, not yet built

The hybrid context engine and the retrieval orchestrator from the architecture above are **all built** as of 2026-04-27. The next phase is stateful memory.

### Shipped

| Phase | Wave | Status | Notes |
|---|---|---|---|
| Week 1-2: Agent core | — | ✅ | ReAct loop, plan→approve→execute, sf CLI tool wrappers |
| Week 3-4: Metadata index (Layer 2) | Wave 1 | ✅ | SQLite, generic schema, `MetadataIndex`, ApexClass/ApexTrigger/CustomObject parsers |
| Week 3-4: Tool wiring | Wave 2 | ✅ | `code_search`, `sf_dependency_graph`, `build_metadata_index` |
| Week 3-4: Apex REFERENCES edges | Wave 2c | ✅ | Class-to-class reference graph |
| Week 5-6: Code vector store (Layer 1) | Wave 3 | ✅ | Gemini `gemini-embedding-001`, hash-gated re-embedding, `semantic_search` |
| Week 5-6: Delta refresh | Wave 4 | ✅ | Tooling-API LastModifiedDate diff, type-isolated deletion |
| Week 5-6: Knowledge base (Layer 3) | Wave 5 | ✅ | 32 hand-authored entries, `knowledge_search`, `embed_knowledge_base` |
| Week 5-6: CustomObject delta | Wave 6 | ✅ | Merged object + max-field timestamps; FK CASCADE on parent delete |
| Week 5-6: Retrieval Orchestrator | Wave 7 | ✅ | `retrieve_context` fans out to all three layers + 1-hop graph enrichment |

Default `pytest` runs **113 tests**; integration + smoke (live-org) suites bring the total to 120.

### Next: Memory tiers (Week 9-10)

Designed against Claude Code's auto-memory practices but with SQLite + vector recall instead of file-per-fact, because the SF agent will accumulate thousands of memories across orgs and sessions. See `docs/sessions/2026-04-26.md` Wave 8 entry for the full design.

- **Working memory:** session conversation + plan snapshots persisted to SQLite (resume-from-crash).
- **Project memory:** `decision / preference / constraint / note` rows with `Why:` + `How to apply:` body convention from Claude Code, scoped by `org_alias`, surfaced through `retrieve_context` as a fourth source.
- **Learning memory:** flagged Project memories exportable to `context/knowledge/entries/` for cross-tenant reuse. Manual curation, not automatic.

### Held / on-deck

- Plan/approve/execute UI (Week 7-8) — backend state machine works; UI is product-layer, deferred until after memory.
- Real-org pressure test of the orchestrator + delta refresh — held until a real client org is available.
- Standard-object delta (Account, Contact extension fields) — needs an EntityDefinition pivot.
- ValidationRule / RecordType / ListView delta — generic story, different parser + Tooling object each.

---

## Update — 2026-04-27: Wave 8 (memory tiers) shipped end-to-end

Memory was the last load-bearing piece of the architecture above. As of 2026-04-27, **all three tiers from "Stateful Memory" — working / project / learning — are live on `main`**. The "Current Phase" section above reflects the plan at the start of the wave; this section captures the actual shipped state.

### Shipped in Wave 8

| Slice | Focus | Commit | Tests delta |
|---|---|---|---|
| 1 | Project memory + orchestrator 4th source | `38c1764` | +27 |
| 2a | Working-memory persistence | `c13d188` | +25 |
| 2b | AWAITING_APPROVAL state + AgentLoop.resume() | `a8d4a43` | +8 |
| 2c | Decay scoring | `ef53ccf` | +6 |
| 2d | Compaction + supersede | `85c68d9` | +13 |
| 3 | Extraction + export + promotion | `2270122` | +39 |

**231 / 231 default tests passing** (was 113 pre-Wave-8). Lint clean on all touched files.

### What the agent now has end-to-end

1. **Project memory** — durable, vector-recalled, scoped per (tenant, org). Surfaced through `retrieve_context` as a 4th source (alongside semantic / literal / knowledge).
2. **Working memory** — every task's full lifecycle (state + transcript) persisted to SQLite. Resume from any non-terminal status with redisplay where appropriate (`AgentLoop.resume(task_id, ...)`).
3. **Decay scoring** — fresh + frequently-recalled memories rank higher; stale ones nudged down without ever being deleted (auditability over reclamation).
4. **Compaction** — automatic detection of similar memories within (scope, type) at cosine ≥ 0.85; agent-proposed merges; supersedes link preserves audit trail.
5. **Extraction** — end-of-session sweep that catches save-worthy moments the agent missed in-flight. LLM-driven, manual user confirmation, confidence-scored.
6. **Export** — Markdown round-trip for transparency, backup, git versioning, cross-machine portability.
7. **Promotion** — manual-curated path from tenant-private project memory to cross-tenant platform knowledge. Tenant-specific-content heuristic gate.

### New CLI surface

```bash
# Wave 8 added a memory subcommand
sf-agent memory extract --task-id <id>         # end-of-session capture
sf-agent memory export   [--type] [--out]      # dump to disk
sf-agent memory promote  --memory-id <id> --category <cat>
```

### New module surface

```
src/sf_dev_agent/
  memory/
    store.py            # MemoryStore — project memory (Wave 8 slice 1)
    working.py          # WorkingMemoryStore — task state + transcript (slice 2a)
    conversation_log.py # list-shaped wrapper that mirrors append() to disk
    extraction.py       # MemoryExtractor — LLM-driven end-of-session capture
    export.py           # MemoryExporter — Markdown round-trip
    promote.py          # MemoryPromoter — drafts knowledge entries
  memory_cli.py         # sf-agent memory <verb> dispatch
```

### Six tools wired through `tools/registry.py`

- `memory_save` (write, plan-approval gated)
- `memory_recall` (read-only; embeds the query via Gemini, mocked in offline mode)
- `memory_list` (read-only, pure local SQLite)
- `memory_compact` (read-only, pure local; pairwise cosine + connected-components BFS)
- `memory_supersede` (write, plan-approval gated)
- `retrieve_context` extended with `memory_type` filter (memory_scope auto-set from `OrgConnection`)

### SQLite tables (all in `default_db_path()`)

```
components               -- metadata index (Wave 1)
relationships            -- dependency graph (Wave 1+2c)
index_runs               -- ingestion provenance (Wave 1)
knowledge_entries        -- bundled platform knowledge (Wave 5)
memories                 -- project memory (Wave 8 slice 1)
tasks                    -- working memory (Wave 8 slice 2a)
conversation_messages    -- working memory transcript (Wave 8 slice 2a)
```

One open of the DB serves every memory tier the agent uses.

### Architectural choices locked in Wave 8

- Multi-tenant schema (`tenant_id` + `org_alias`) from day one even though runtime is single-tenant. Avoids future migration.
- Memory recall via the orchestrator, not always-load. Volume problem solved by retrieval, not by capping count.
- Persistence is best-effort: working-memory writes are wrapped in try/except with `logger.exception` — a SQLite hiccup logs but never drops in-memory data.
- Decay scoring is multiplicative + clipped: `score = clip(cosine * (1 + decay_factor), 0, 1)`. Cosine dominates; decay nudges by ≤10% (recency penalty) + ≤5% (usage boost).
- Compaction preserves history: `superseded_by` is a soft tombstone; old rows stay on disk, hidden from `recall`/`list` by default.
- Promotion has a heuristic gate for tenant-specific content (org alias / instance URL / Salesforce-shaped IDs); soft-blocks unless `--force`, and `--force` inlines the warnings into the draft as a REVIEW comment.

### Reverted design pivots (do not relitigate)

Two design pivots were proposed and reverted during Wave 8. Documented here so they don't get re-opened:

1. **Redis for working memory + per-user SQLite for project memory.** Reverted: "stick to the original plan, we need a working model now, scale later."
2. **Full Postgres / pgvector migration absorbed into Wave 8.** Analyzed scope (~3 slices, ~100 `db_path` references touched). Reverted same reason. SQLite stays canonical; Postgres is the multi-tenant scale path, not the MVP.

### Where to look next

- **Backlog**: [`docs/ROADMAP.md`](ROADMAP.md) — full backlog by plane, with priority and effort estimates. Includes both architecture gaps (control / execution plane) and concrete UX deliverables (`sf-agent doctor`, `sf-agent resume`, auto-warm context engine, extract nudge, persistent REPL).
- **Wave 8 design log**: [`docs/sessions/2026-04-27.md`](sessions/2026-04-27.md) — full narrative of the day Wave 8 shipped. Captures the threshold-tuning incident in slice 2d, the JSON-quoting parser extension in slice 3b, and the two reverted design pivots.
- **Wave 8 design doc (final)**: in the auto-memory store at `~/.claude/projects/.../memory/wave8_memory_design.md` — locked design calls + as-shipped state.

### What's next

Wave 8 closes the memory architecture. Remaining roadmap items, ordered by leverage:

1. **UX wins** — phase B in `ROADMAP.md`: ✅ shipped end-to-end. `sf-agent doctor` (`e67a161`), auto-warm context engine + staleness check (`38f6c19`), `sf-agent resume` CLI verb (`e8d89bb`), and the docs/alias refresh. Single-binary install — no `uv run` prefix needed; `sf-agent` and `sfagent` both work.
2. **Persistent REPL** — phase C in `ROADMAP.md`: ✅ shipped end-to-end. See "Update — 2026-04-28" section below.
3. **Real-org pressure test** of the now-4-layer orchestrator (held until org access).
4. **Production planes** — control + execution. Auth, tenancy, orchestration API, structured audit, billing, containerization, network egress lockdown. These are product/platform work, not agent work; they live in `ROADMAP.md` Part 1.

---

## Update — 2026-04-28: Phase C shipped end-to-end

The persistent REPL — Claude-Code-style terminal session — is fully built. Six independently-shipped slices on `main`:

| Slice | What landed | Commit |
|---|---|---|
| C.1 | REPL skeleton + 12 slash commands (`prompt_toolkit`, FileHistory at `~/.sf-agent/history`, WordCompleter, bottom_toolbar status line, dispatcher split from prompt loop for testability) | `b557465` |
| C.2 | Streaming output: new `chat_stream()` on `LLMProvider` yielding `StreamChunk` discriminated union (`TEXT_DELTA`, `TOOL_USE_START`, `TOOL_USE_DELTA`, `TOOL_USE_END`, `STOP`); real Gemini streaming via `generate_content_stream`; default fallback wraps `chat()` so every other provider gets free pseudo-streaming; agent unified through `chat_stream` + `consume_stream` | `e5e006f` |
| C.3 | ESC / Ctrl+C interrupt: `InterruptListener` daemon thread polling stdin (`msvcrt` on Windows, `termios` + `select` on POSIX, no-op on non-TTY); streaming `on_text` callback raises `InterruptedError`, caught alongside `KeyboardInterrupt`; second poll-point between LLM stream and tool dispatch so ESC during the response cancels emitted tools; synthetic `<user pressed ESC>` message preserves context | `8974276` |
| C.4 | Resume-by-LLM-intent via 3 tools: `list_resumable_tasks` + `get_task_summary` (read-only) and `request_resume` (intercepted in `AgentLoop._execute_tool` like `submit_plan`); REPL reads `agent.resume_requested` after `run()` returns and dispatches `AgentLoop.resume(...)` so resumed task lands in one keystroke; `ToolRegistry` now takes optional `WorkingMemoryStore` handle | `1c21092` |
| C.5 | Extract nudge at `/quit`: soft prompt `[yes / skip / no-and-stop-asking]` walks completed tasks through `MemoryExtractor` candidate-by-candidate; per-(tenant, org) sentinel file (same pattern as B.2 warmup); one-shot CLI runs don't get the nudge | `b06240e` |
| C.6 | Documentation refresh: ROADMAP / PROJECT_SUMMARY / README updated with as-built notes | this commit |

**Test count: 369 passing** (231 at end of Wave 8 → +138 across phase B + phase C). No regressions across the suite during phase C.

### Architectural choices locked in Phase C

- **Streaming abstraction is provider-agnostic.** `StreamChunk` + `consume_stream` decouple "real streaming" from "fallback pseudo-streaming". Adding real streaming for Anthropic / OpenAI is opt-in — override `chat_stream` in the provider — and doesn't break anything.
- **Interrupt is poll-based, not signal-based.** Background thread sets a `threading.Event`; the on_text callback polls and raises. Avoids the pitfalls of signal handling on Windows (where SIGINT delivery is patchy) and works in non-TTY environments by no-op'ing.
- **Resume-by-intent uses an intercepted tool, not a regex classifier.** The LLM does its own intent recognition. Intercepting `request_resume` at the tool-call boundary is the clean cut point because resume requires *replacing* the current `AgentLoop`, which an in-loop tool executor can't do.
- **Extract nudge is end-of-session, not per-task.** Single soft prompt at `/quit` keeps approval fatigue low. The earlier "per-task extract nudge" idea was dropped in the C.5 design.
- **REPL dispatcher is split from `prompt_toolkit`.** All routing logic (`_dispatch`, `_dispatch_slash`, `_dispatch_agent`) is testable without touching the interactive layer; `prompt_toolkit` only owns the input line.

### Where to look next (Phase C)

- **Phase C session log**: [`docs/sessions/2026-04-28.md`](sessions/2026-04-28.md) — narrative of the C.3–C.6 work and the design choices behind them.
