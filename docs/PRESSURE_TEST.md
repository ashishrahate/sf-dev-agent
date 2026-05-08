# Real-org Pressure-Test Protocol

A re-runnable, manually-driven validation pass that takes the 4-layer retrieval orchestrator + ingestion pipelines + delta refresh end-to-end against a live Salesforce org. Authored 2026-05-07; first execution targets `AgentforceOrg`.

This protocol is **read-only** through Phase 4. Phase 5 makes a single reversible change in the org and reverts it.

---

## Two run modes

| Mode | Target | Goal | When |
|---|---|---|---|
| **Correctness pass** | Small dev org (≤50 components per type) | Validate every code path runs end-to-end against real metadata | First-time validation, regression after parser/orchestrator changes |
| **Scale stress** | 1K+ component org | Surface scale-shape issues: timing, batching, memory, embedding cost, dedup at high cardinality | Once a larger org is authorized; re-run before any production deploy |

Both modes use the same protocol — only the target org and pass/fail thresholds differ.

---

## Connected-orgs snapshot (as of 2026-05-07)

| Alias | ApexClasses | Status | Use |
|---|---|---|---|
| `AgentforceOrg` | 22 (`SELECT COUNT() FROM ApexClass`) | Connected, default DevHub | Configured in `.env` as `SF_ORG_ALIAS`. Correctness-pass target today. |
| `SalesKPIs` | 2 | Connected | Secondary; too small for protocol |
| `AgentforceHAckOrg` | — | DomainNotFoundError | Skip |

Switching target orgs: change `SF_ORG_ALIAS` in `.env` to the new alias. No code changes — the architecture is org-agnostic.

---

## Pre-flight

Before Phase 1:

1. `sf-agent doctor` — confirms green on Python / uv / Node / sf CLI / git / LLM API key.
2. `sf org display --target-org $SF_ORG_ALIAS` — confirms the org is connected, prints instance URL + API version.
3. `sf data query --query "SELECT COUNT() FROM ApexClass" --target-org $SF_ORG_ALIAS --json` — record baseline component counts (repeat for ApexTrigger, CustomObject, ValidationRule, RecordType, Flow, LightningComponentBundle).
4. Open the session log: `docs/sessions/<YYYY-MM-DD>.md` (stub provided for first run).

If `sf-agent doctor` is red on any required check: stop, fix, restart pre-flight.

---

## Phase 1 — Ingestion pipeline against live org

**Goal:** validate every parser (Wave 1 + 2c + D-series) runs against real metadata. No FK orphans, no parser exceptions, every expected component type populated.

### How

1. Confirm `.env`: `SF_ORG_ALIAS=<target>`, `LLM_PROVIDER=...` (one of anthropic / openai / gemini), corresponding API key set.
2. Launch the REPL: `sf-agent`.
3. Run the index from inside the REPL: `/index`. Capture the wall-clock and any per-type breakdown emitted.
4. Alternative without REPL: `uv run python -m sf_dev_agent build-index` (or whatever the CLI verb is on `main` — check `__main__.py`). The REPL `/index` is the canonical path.

### Verify in SQLite (`paths.default_db_path()`)

Open the DB with the `sqlite3` CLI or any client. Run:

```sql
-- Component counts per type
SELECT type, COUNT(*) AS n
FROM components
GROUP BY type
ORDER BY n DESC;

-- Expect non-zero rows for: ApexClass, ApexTrigger, CustomObject, CustomField,
-- ValidationRule, RecordType, Flow, LightningComponentBundle.
-- Some types may legitimately be zero in a small org; capture which.

-- Edge counts per type
SELECT edge_type, COUNT(*) AS n
FROM relationships
GROUP BY edge_type
ORDER BY n DESC;

-- Expect REFERENCES, REFERENCES_OBJECT, VALIDATES_ON, RECORD_TYPE_OF,
-- TRIGGERS_ON edges as the org actually contains them.

-- Parent FK integrity (must be 0)
SELECT COUNT(*) AS orphans
FROM components
WHERE parent_id IS NOT NULL
  AND parent_id NOT IN (SELECT id FROM components);

-- Latest index run
SELECT *
FROM index_runs
ORDER BY started_at DESC
LIMIT 1;
```

### Cross-check against the org

For each component type with a non-zero local count, run the corresponding SOQL on the live org and confirm the numbers line up:

```bash
sf data query --query "SELECT COUNT() FROM ApexClass" --target-org $SF_ORG_ALIAS --json
sf data query --query "SELECT COUNT() FROM ApexTrigger" --target-org $SF_ORG_ALIAS --json
# … and so on
```

Counts won't match exactly for every type (managed-package classes, system classes, retrieval scope) — but the local count should never exceed the org count, and the gap should be explainable.

### Stop conditions

- Any parser raises an unhandled exception (a `parse_error` in metadata is acceptable; a stack trace exiting the indexer is not).
- FK orphan count > 0.
- A type that should populate (org has the type, parser exists) shows zero rows.
- Index-run row missing or marked failed.

---

## Phase 2 — Embedding + Layer-1 vector store

**Goal:** validate Wave 3 + hash-gating. Every component gets a 3072-d Gemini vector exactly once; second run is a no-op.

### How

1. Run `embed_metadata_index` (REPL `/index` may chain it; if not, run it as a separate command).
2. Capture: total embedding API calls, total tokens charged (Gemini quota dashboard), wall-clock.
3. Re-run **immediately** without changing anything in the org.
4. Capture the same metrics — expect ~zero new API calls and ~instant completion.

### Verify

```sql
-- Coverage: every component should have a hash recorded
SELECT
  COUNT(*) AS total,
  SUM(CASE WHEN embedded_source_hash IS NOT NULL THEN 1 ELSE 0 END) AS embedded
FROM components;

-- Spot-check: pick one ApexClass, confirm hash matches its content
SELECT id, embedded_source_hash, length(embedding)
FROM components
WHERE type = 'ApexClass'
LIMIT 5;
```

For the spot-check: take an ApexClass row's `content`, compute its `sha256` (e.g. `python -c "import hashlib; print(hashlib.sha256(open('classfile').read().encode()).hexdigest())"` or via the existing `hash_text` helper), confirm it matches `embedded_source_hash`. If `embedding` is a BLOB, `length()` should be 3072 floats × 4 bytes = 12,288 bytes (or whatever the storage format dictates — confirm against `embedders/gemini.py`).

### Stop conditions

- Repeated runs re-embed unchanged rows (hash-gate broken).
- Any embedding API failure not retried + recovered.
- Coverage gap on a type that should be embedded (e.g. ApexClass with NULL hash after the run).

---

## Phase 3 — Retrieval quality battery

**Goal:** validate Wave 7 orchestrator. Each query returns sensible top hits, layers compose correctly, dedup works, graph enrichment surfaces 1-hop neighbors.

### Query battery

Run each via `retrieve_context(query=..., top_k=10)`. The tool is exposed in the REPL through the agent; alternatively call the orchestrator function directly in a Python REPL bound to the same SQLite DB.

| # | Query | Expected shape |
|---|---|---|
| 1 | `AccountTriggerHandler` | Layer-2 literal hit on the class itself; Layer-1 vector hits on related classes; graph 1-hop to AccountTrigger via REFERENCES |
| 2 | `validate annual revenue` | Layer-3 KB hits on validation patterns; if any VR exists in org, Layer-1 hit on it |
| 3 | `RecordSelectorController` | Layer-2 literal + Layer-1 vector hits; graph enrichment surfaces test class |
| 4 | `trigger handler pattern` | Layer-3 KB dominant; Layer-1 surfaces `AccountTriggerHandler` |
| 5 | `governor limits SOQL` | Layer-3 KB only |
| 6 | `what objects reference Account` | Graph traversal exercise; Layer-2 metadata index dominant |

### Capture per query

For each, record in the session log:

- Hits-per-layer count (e.g. `vector=4 literal=2 kb=3 memory=0`).
- `estimated_tokens` total.
- Top-3 `component_id`s with their scores.
- Eyeball: is the top hit the *right* hit?
- Any duplicates surviving across layers (dedup leak)?
- Any graph 1-hop neighbors that look wrong (over-eager traversal)?

### Stop conditions

- An obviously-correct query returns the wrong top hit.
- Layers don't compose (e.g. literal match for a known class name returns nothing while vector returns weak partials).
- Dedup leaks duplicates across layers.
- `estimated_tokens` is wildly wrong (e.g. 10x what content would imply).

---

## Phase 4 — Agent task end-to-end (REPL, read-only)

**Goal:** validate the whole stack — REPL → AgentLoop → tools → orchestrator → SQLite + LLM. No writes; no plan approvals.

### Tasks (sequential in one REPL session)

1. **"Tell me about AccountTrigger in this org. What object is it on, what events does it fire on, and what classes does it call into?"** — exercises `sf_metadata_describe` + `code_search` + graph traversal.
2. **"List every Apex class whose name contains 'Handler'. For each, summarize what it does in one line."** — exercises `code_search` + `semantic_search`.
3. **"Are there any validation rules on Account? What do they check?"** — exercises ValidationRule parser path + `retrieve_context`.
4. **"Show me the Lightning components in this org and what Apex methods they call."** — exercises LWC parser + REFERENCES edge.
5. **`/quit`** — confirm extract-nudge fires; accept it; verify any candidate memories surface and are accepted/rejected per intent.

### Capture per turn

For each turn:

- Which tool the agent picked first (`retrieve_context` orchestrator vs. low-level primitive). Was it the right tool for the question?
- Token count per LLM call (eyeball; no formal accounting yet — just note egregious cases).
- Streaming UX: text renders smoothly, tool-call headers/results render via `repl_ui.py`, no torn output.
- Plan-approval gate: if the agent submits a plan, **REJECT it** (these prompts are read-only-shaped; a plan submission is itself a signal worth noting).
- Final answer quality: did the agent answer the question, or hallucinate?

### Stop conditions

- Any tool call fails on real-org data (KeyError, None traversal, network panic).
- Agent picks the wrong tool repeatedly (e.g. doesn't notice it has `retrieve_context` and re-implements the search via 5 primitive calls).
- UI artifacts: torn streaming, missing tool footer, spinner stuck.
- Extract-nudge fails to fire or surfaces malformed candidates.

---

## Phase 5 — Delta refresh

**Goal:** validate Wave 4 LastModifiedDate diff. Exactly one row's timestamp advances after a targeted change; type-isolation prevents collateral.

**Skip this phase if you prefer a zero-touch run.** All correctness signal lives in Phases 1-4.

### How

1. Note current `MAX(last_modified_at)` per type from the components table:
   ```sql
   SELECT type, MAX(last_modified_at) FROM components GROUP BY type;
   ```
2. Make one small, safe change in the org via Setup UI:
   - Edit a description field on an existing ApexClass, **or**
   - Add an inactive ValidationRule on a non-critical object.
   - Reversibility is mandatory — pick something you can undo in one click.
3. Run the delta refresh path. (Check `context/delta.py` for the current invocation — likely `build_metadata_index --delta` or a dedicated tool.)
4. Verify exactly that one row's `last_modified_at` advanced; others unchanged. Verify type-isolation: a CustomField-only refresh does not touch ApexClass rows (and vice versa).
5. Revert the org change. Re-run delta refresh; confirm the row reverts to its prior state (or marked deleted, depending on the change shape).

### Stop conditions

- Delta refresh re-ingests too much (hash-gate / type-isolation broken).
- Delta refresh re-ingests too little (filter wrong; misses the actual change).
- Type-isolation safety violated: a refresh of one type alters rows of another type.

---

## Capture template

Per phase, record the following in the day's session log:

```markdown
### Phase N — <name>

**Status:** ✅ pass | ❌ fail | ⚠️ partial

**Wall-clock:** <duration>

**What we ran:** <command(s) or REPL turns>

**Observed:**
- <metric 1> = <value>
- <metric 2> = <value>
- ...

**Surprises / gaps:**
- <surprise 1>

**Next-action items raised:**
- [ ] <item 1>
```

End-of-day section to add at the bottom of the session log:

```markdown
## Wrap-up

**Overall:** <pass / partial / blocked>

**New ROADMAP entries:**
- <entry 1>
- <entry 2>

**New project-memory items:**
- <item 1>

**Re-run plan:**
- [ ] Re-run against <larger org> when authorized.
- [ ] Investigate <surprise> before next pressure-test cycle.
```

---

## Failure-mode reference

If you hit one of these, you've already seen the right thing — they're the surfaces this protocol is built to find.

| Symptom | Likely cause | Where to look |
|---|---|---|
| Local component count > org SOQL count for a type | Stale index, retrieval scope mismatch, packaged metadata included | `context/parsers/<type>.py`, `context/delta.py` |
| Local count << org count | Parser missed structurally-different files, retrieve manifest narrow | `context/parsers/<type>.py`, retrieve manifest in `tools/registry.py` |
| FK orphans after Phase 1 | Two-pass ingestion broken; relationship referencing missing component | `context/index.py` (upsert order), specific parser's relationship emit |
| Hash-gate re-embeds on second Phase 2 run | `embedded_source_hash` not being written, or hash computation source-dependent | `context/embedders/gemini.py`, `MetadataIndex.embed_pending` |
| Vector store empty embeddings | Gemini API key missing/invalid, network egress blocked | `.env`, `embedders/gemini.py` initialization |
| Layer-2 literal misses on known class name | Tokenization or case-sensitivity in `code_search` | `tools/registry.py:_exec_code_search`, indexed `searchable_text` column |
| Graph 1-hop returns wrong neighbors | Edge-direction confusion in orchestrator's enrichment pass | `context/orchestrator.py`, edge query in `retrieve_context` |
| Dedup leaks duplicates across layers | Component-id mismatch between Layer-1 vector and Layer-2 literal hits | `context/orchestrator.py:_dedupe`, component-id construction in parsers |
| Agent re-implements `retrieve_context` via primitives | System prompt doesn't surface the tool; or tool description weak | `prompts/system_prompt.md`, `retrieve_context` tool description in `tools/registry.py` |
| Streaming output torn | `consume_stream` callback contention, `repl_ui` console flush timing | `repl_ui.py`, `providers/base.py:consume_stream` |
| Delta refresh ignores a real change | Tooling-API filter wrong, watermark not advancing | `context/delta.py`, `MetadataIndex.last_modified_at` math |
| Delta refresh deletes too much | Type-isolation safety check broken | `context/delta.py:_purge_deleted` (or wherever the delete-marking lives) |

---

## Re-running this protocol

This document is the canonical protocol. When a larger org is authorized:

1. Update `.env`: `SF_ORG_ALIAS=<new-alias>`.
2. Update the connected-orgs snapshot table at the top of this doc.
3. Re-run pre-flight + Phases 1-5 against the new alias.
4. Open a fresh session log: `docs/sessions/<YYYY-MM-DD>.md`.
5. If the protocol itself needs updates (new failure modes, new component types, new layers), edit this doc.

The protocol is independent of any single org. Promote it from "first validation pass" to "scale stress" by changing the target — the procedure is the same.
