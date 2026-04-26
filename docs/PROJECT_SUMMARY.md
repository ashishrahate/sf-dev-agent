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
