# sf-context MCP server

Gives any MCP-capable coding agent a persistent understanding of a Salesforce org: what exists, what depends on what, org-specific decisions, and curated best practices. It never writes to the org.

| Server | Role | Use it for |
|---|---|---|
| **sf-context** (this repo) | *Understand* the org | "What touches Account?", dependency checks before a change, governor-limit/anti-pattern guidance, remembered decisions |
| **Salesforce DX MCP** (`@salesforce/mcp`) | *Act* on the org | Deploy/retrieve metadata, run SOQL, run Apex tests, manage orgs/users |

## Install

```bash
# from the repo root: MCP SDK + local embedder (no API key needed)
uv sync --all-packages --all-extras

uv run sf-context-mcp --warmup          # one-time ~67 MB model download
uv run python scripts/mcp_preflight.py --org <alias>   # what's ready / missing
```

Published install (later): `pip install "sf-context-engine[server]"`.

## Run

```bash
sf-context-mcp --org <alias>            # stdio, what agents launch
```

| Setting | Flag / env | Default |
|---|---|---|
| Org alias (must be authenticated in `sf`) | `--org` / `SF_ORG_ALIAS` | required |
| Tenant id for memory scoping | `--tenant` / `SF_CONTEXT_TENANT` | `local-dev` |
| State directory | `SF_CONTEXT_HOME` | `~/.sf-context` |
| SQLite path | `--db` / `SF_CONTEXT_DB` | `<state dir>/metadata_index.db` |
| Embedder | `SF_CONTEXT_EMBEDDER` = `fastembed`, `gemini`, `mock` | `fastembed` when installed |
| Embedding model | `SF_CONTEXT_EMBEDDER_MODEL` | `BAAI/bge-small-en-v1.5` |

First use per org: call `build_metadata_index`, then `embed_metadata_index` and `embed_knowledge_base` (or let the agent do it, because `index_status` tells it what is missing).

## Client configs

Copy the matching file and adjust the alias and repo path:

| Client | Config file | Put it at |
|---|---|---|
| Claude Code | `configs/claude-code.mcp.json` | `.mcp.json` in the project, or `claude mcp add` |
| Cursor | `configs/cursor.mcp.json` | `.cursor/mcp.json` |
| VS Code (Copilot) | `configs/vscode.mcp.json` | `.vscode/mcp.json` |

On Windows the DX server is launched through `cmd /c npx` (as in these files); on macOS/Linux use `"command": "npx"` with the same arguments. Each config starts **both** servers. For the A/B/C comparison in `TEST_PLAN.md`, delete the entry you want off.

## Tools (12, about 2,100 tokens of definitions)

Query: `retrieve_context` (start here), `code_search`, `semantic_search`, `dependency_graph`, `knowledge_search`.
Index: `index_status`, `build_metadata_index`, `embed_metadata_index`, `embed_knowledge_base`.
Memory: `memory_save`, `memory_recall`, `memory_list`.

Result budgets: `retrieve_context` defaults to 2,500 tokens; `code_search` with source returns at most 10 hits of 40 lines.

## Changing embedder

Vectors are tied to the model that made them. Switching `SF_CONTEXT_EMBEDDER` or the model makes embed and search calls return `embedder_mismatch`; call `embed_metadata_index` / `embed_knowledge_base` with `reset_embeddings=true` to recompute.
