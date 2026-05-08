# Architecture — as shipped (2026-05-01)

Snapshot of the as-built system through Waves 1-8 + Phase B + Phase C + D-series. Every box below has code on `main`. Held / on-deck items (Tooling API `SymbolTable` enrichment, tree-sitter Apex AST, REPL UI v2, plan/approve/execute UI, ListView parser, control + execution planes) are deliberately not drawn — see [`ROADMAP.md`](ROADMAP.md) for those.

For the narrative behind each layer see [`PROJECT_SUMMARY.md`](PROJECT_SUMMARY.md). For per-day build history see [`sessions/`](sessions/).

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                              USER SURFACES                                   │
│                                                                              │
│  $ sf-agent              # persistent REPL (prompt_toolkit)                  │
│  $ sf-agent doctor       # B.1 — env / sf CLI / config preflight             │
│  $ sf-agent resume <id>  # B.3 — one-shot resume from OS shell               │
│  $ sf-agent setup        # interactive wizard                                │
│  $ sf-agent memory {extract|export|promote}     # W8.3 maintenance verbs    │
│                                                                              │
│   REPL features (Phase C):                                                   │
│   ├─ streaming output       (C.2 chat_stream / StreamChunk)                  │
│   ├─ ESC interrupt          (C.3 InterruptListener daemon thread)            │
│   ├─ resume-by-LLM-intent   (C.4 list/summary tools + intercepted resume)    │
│   ├─ /quit extract nudge    (C.5 per-(tenant,org) sentinel)                  │
│   └─ Claude-Code-style UI   (D.5 repl_ui.py: header/ok/error/blocked+spin)   │
│       12 slash commands • FileHistory • WordCompleter • bottom_toolbar       │
└──────────┬───────────────────────────────────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                            AGENT CORE   (agent.py)                           │
│                                                                              │
│   AgentLoop  — composable phases (refactored in W8.2b):                      │
│     plan ──► AWAITING_APPROVAL ──► execute ──► (success | error | resumed)   │
│                                                                              │
│   classmethod resume(task_id, ...) — full redisplay of working memory        │
│                                                                              │
│   Interrupt: msvcrt (Win) / termios+select (POSIX) / no-op (non-TTY)         │
│   Streaming: on_text → repl_ui.render_streaming_text                         │
│   Best-effort persistence: every working-memory write try/except + logged    │
│                                                                              │
│  prompts/system_prompt.md  ←  {{INDEX_FRESHNESS}}  (B.2 staleness)           │
└──┬─────────────────────┬──────────────────────────┬──────────────────────┬───┘
   │                     │                          │                      │
   ▼                     ▼                          ▼                      ▼
┌──────────┐  ┌─────────────────────────┐  ┌──────────────────┐  ┌────────────┐
│ PROVIDERS│  │      TOOL REGISTRY       │  │  CONTEXT ENGINE  │  │   MEMORY   │
│          │  │     (tools/registry.py)  │  │ (3 layers + 4th) │  │ (Wave 8)   │
│ Claude   │  │                          │  │                  │  │            │
│ Gemini ◄─┼──┤  26 tools registered     │  │  retrieve_context│  │ project    │
│ OpenAI   │  │                          │  │    │   fans out  │  │ working    │
│          │  │  SF CLI (5):  describe / │  │    ▼             │  │ learning   │
│ chat_stream│ │     soql / retrieve /   │  │  4 sources       │  │ (=KB)      │
│ → StreamChunk│   deploy / test_run     │  │  in parallel     │  └────────────┘
│  union   │  │  Filesystem (3): read /  │  │                  │
│          │  │     write / bash         │  │                  │
└──────────┘  │  Retrieval (6): retrieve │  │                  │
              │     _context, code_search│  │                  │
              │     sf_dependency_graph, │  │                  │
              │     knowledge_search,    │  │                  │
              │     semantic_search,     │  │                  │
              │     check_index_freshness│  │                  │
              │  Memory (5):  save /     │  │                  │
              │     recall / list /      │  │                  │
              │     compact / supersede  │  │                  │
              │  Index admin (3): build_ │  │                  │
              │     metadata_index,      │  │                  │
              │     embed_metadata_index,│  │                  │
              │     embed_knowledge_base │  │                  │
              │  Resume (3): list /      │  │                  │
              │     summary / request_   │  │                  │
              │     resume (intercepted) │  │                  │
              │  Gate (1): submit_plan   │  │                  │
              │     (intercepted)        │  │                  │
              │                          │  │                  │
              │  _SF_TOOLS frozenset →   │  │                  │
              │  mock_responses.py shim  │  │                  │
              │  in offline mode         │  │                  │
              └──────────────────────────┘  │                  │
                                            │                  │
   ┌────────────────────────────────────────┘                  │
   │                                                           │
   ▼                                                           │
┌────────────────────────────────────────────────────────────┐ │
│            CONTEXT ENGINE — 4-source orchestrator           │ │
│                       (orchestrator.py)                     │ │
│                                                             │ │
│  ┌────────────────┐  ┌────────────────┐  ┌──────────────┐  │ │
│  │ Layer 1: VECTOR│  │ Layer 2: INDEX │  │ Layer 3: KB  │  │ │
│  │  semantic      │  │  graph         │  │  bundled     │  │ │
│  │  search via    │  │  traversal     │  │  knowledge   │  │ │
│  │  Gemini embed  │  │  + literal     │  │  entries     │  │ │
│  │  (3072-d)      │  │  match         │  │              │  │ │
│  │  hash-gated    │  │                │  │  store.py +  │  │ │
│  │  re-embed      │  │  index.py      │  │  entries/    │  │ │
│  │                │  │  schema.sql    │  │  + 32 hand-  │  │ │
│  │  embedders/    │  │  delta.py      │  │  authored    │  │ │
│  │   gemini.py    │  │                │  │              │  │ │
│  └────────────────┘  └────────────────┘  └──────────────┘  │ │
│                                                             │ │
│  ┌────────────────────────────────────────────────────────┐│ │
│  │ Layer 4 (W8 slice 1): MEMORY  ←──────────────────────────┘
│  │   memory_type filter • memory_scope auto-set            ││
│  │   from OrgConnection                                    ││
│  └────────────────────────────────────────────────────────┘│
│                                                             │
│  + 1-hop graph enrichment (W7) over relationships           │
└──────┬──────────────────────────────────────────────────────┘
       │ ingestion: PARSERS  (two-pass: components → relationships)
       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                       PARSERS  (context/parsers/, 8 types)                  │
│                                                                             │
│   ApexClass (apex_class.py + _apex_refs.py REFERENCES extractor)            │
│   ApexTrigger (apex_trigger.py)                                             │
│   CustomObject + CustomField (custom_object.py, parent_id FK + CASCADE)     │
│   ValidationRule (D.1, edge: VALIDATES_ON)                                  │
│   RecordType    (D.2, edge: RECORD_TYPE_OF)                                 │
│   Flow          (D.3, edges: TRIGGERS_ON, REFERENCES, REFERENCES_OBJECT,    │
│                              REFERENCES_FLOW)                               │
│   LightningComponentBundle (D.4, bundle-shaped, sibling .js scan →          │
│                              REFERENCES + REFERENCES_OBJECT)                │
│                                                                             │
│   Adding a new type = 1 file in parsers/ + 1 import line. No DDL.           │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│                       PERSISTENCE — single SQLite file                      │
│                       (paths.default_db_path())                             │
│                                                                             │
│   components            ┐                                                   │
│   relationships         │  metadata index + dep graph (W1, W2c)             │
│   index_runs            ┘                                                   │
│   knowledge_entries        bundled KB (W5)                                  │
│   memories                 project memory (W8.1, vector recalled, decay)    │
│   tasks                    working memory state machine (W8.2a)             │
│   conversation_messages    transcript (W8.2a, FK CASCADE on task_id)        │
│                                                                             │
│   Hash-gated re-embedding shared across MetadataIndex / KB / MemoryStore    │
│   Decay: clip(cosine·(1+decay_factor), 0, 1)  — never deletes               │
│   Compaction: superseded_by soft tombstone (W8.2d)                          │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│                            EXTERNAL SYSTEMS                                 │
│                                                                             │
│   Salesforce org  ──────►  sf CLI v2  (sf_metadata_describe, soql,          │
│   (default: AgentforceOrg)             retrieve, source deploy, test run)   │
│                                                                             │
│   Tooling API     ──────►  delta.py LastModifiedDate diff (W4)              │
│                            [held: SymbolTable enrichment for Apex]          │
│                                                                             │
│   Gemini API      ──────►  gemini-embedding-001 (3072-d) for vectors        │
│                            chat / chat_stream for LLM provider              │
│   Anthropic API   ──────►  Claude provider (chat_stream)                    │
│   OpenAI API      ──────►  OpenAI provider                                  │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Test footprint

- **388 default tests** passing (`pytest`)
- **6 integration tests** (live org, opt-in via `pytest -m integration`)
- **1 smoke test** (live org, opt-in via `pytest -m smoke`)
- Total: **395 / 395**
