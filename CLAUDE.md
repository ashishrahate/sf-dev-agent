# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository location

The project root is `sf-dev-agent/` (one level below the workspace directory `SF DEV AGENT/`). The git repo, `pyproject.toml`, and all source live there — run every command below from `sf-dev-agent/`.

## Commands

```bash
uv sync                             # install deps into .venv
uv pip install -e '.[all]'          # editable install + all 3 provider SDKs ([anthropic] / [openai] / [gemini] for one)

uv run pytest                       # default suite (excludes integration + smoke via addopts)
uv run pytest tests/test_agent_approval.py            # one file
uv run pytest tests/test_agent_approval.py::test_name # one test
uv run pytest -m integration        # live-org tests (needs a real org configured)
uv run pytest -m smoke              # live-org agent tests that burn LLM tokens

uv run ruff check .                 # lint (rules: E, F, I, N, W, UP; line-length 100)
uv run ruff format .                # format

sf-agent doctor                     # probe Python/uv/Node/sf CLI/git/LLM key
sf-agent setup                      # interactive wizard — writes .env
sf-agent "<request>"                # one-shot task
sf-agent                            # interactive REPL (requires a TTY)
sf-agent --mock-org "<request>"     # stub all sf CLI calls; LLM still real
```

After an editable install the `sf-agent` / `sfagent` binaries are on PATH — no `uv run` prefix needed for them. Tests never require an editable install (`pythonpath = ["src"]` in pyproject).

## Runtime configuration

- `.env` (gitignored) supplies exactly one LLM API key (`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GOOGLE_API_KEY`) and `SF_ORG_ALIAS`. Provider is auto-detected from whichever key is set; `LLM_PROVIDER` / `--provider` disambiguate when several are set.
- Org type, instance URL, and API version are derived at runtime via the `sf` CLI (`sf_config.py`) and `workspace/sfdx-project.json` — env vars / CLI flags only override.
- The agent reads/writes SFDX metadata under `workspace/` (or `$AGENT_WORKSPACE`). This directory is **not** checked in; it is created/populated at runtime. Path resolution is centralized in `paths.py` (`repo_root()`, `agent_workspace()`).
- All persistent state is one SQLite file at `sf_dev_agent.context.default_db_path()` — metadata index, dependency graph, bundled knowledge base, project memory, and working-memory task state/transcripts share it. Schema in `src/sf_dev_agent/context/schema.sql`.

## Architecture

Provider-agnostic CLI agent that runs a **plan → approve → execute** loop against a live Salesforce org. Authoritative design docs: `docs/ARCHITECTURE.md` (as-shipped box diagram), `docs/PROJECT_SUMMARY.md` (narrative), `docs/ROADMAP.md` (backlog), `docs/sessions/` (per-day logs). Note these docs carry stale test counts — trust `uv run pytest` over any number written in prose.

### The approval gate is the core safety invariant

`agent.py` splits tools into `READ_ONLY_TOOLS` and `WRITE_TOOLS` frozensets. During Phase 1 (planning) any write tool (`file_write`, `sf_source_deploy`, `sf_apex_execute`, `sf_test_run`, `bash`, …) is hard-blocked and returns an error to the LLM. Writes only unlock after the user answers `yes` at the `AWAITING_APPROVAL` gate. There is no `--yes` bypass flag. When adding a tool, classify it into exactly one set.

### Layer map

- **`__main__.py`** — arg parsing + subcommand dispatch. `setup`, `memory`, `doctor`, `resume`, `audit` are special-cased *before* `argparse` and delegate to their own `*_cli.py` / wizard modules.
- **`agent.py` — `AgentLoop`** — ReAct loop with composable phases; `AgentLoop.resume(task_id)` classmethod replays working memory. Interrupt handling is platform-split (`interrupt.py`: msvcrt on Windows, termios+select on POSIX, no-op off-TTY). Streaming renders via `repl_ui.py`.
- **`repl.py` / `repl_commands.py` / `repl_ui.py`** — persistent `prompt_toolkit` REPL, 12 slash commands, streaming + ESC interrupt. Falls back to one-shot loop when stdin isn't a TTY.
- **`providers/`** — `base.LLMProvider` ABC (`chat`, `chat_stream` → `StreamChunk` union). `anthropic_provider.py` / `openai_provider.py` / `gemini_provider.py`. `create_provider()` + `PROVIDERS` registry in `providers/__init__.py`. Each provider SDK is an optional extra — importing an uninstalled one raises `ImportError`, surfaced as "Provider not installed".
- **`tools/registry.py` — `ToolRegistry`** — every tool = `ToolDefinition` (name + JSON schema) + executor callable, registered in `_register_builtin_tools()`. Most write tools shell out to `sf` (uses `sf.cmd` on Windows). `_SF_TOOLS` frozenset lists calls intercepted by `tools/mock_responses.py` in `--mock-org` mode (includes Gemini-embedding tools, to avoid burning quota offline).
- **`context/` — 4-layer hybrid context engine**, fanned out by `orchestrator.py` `retrieve_context`:
  1. Vector store — Gemini `gemini-embedding-001` (3072-d), hash-gated re-embed (`embedders/`)
  2. Metadata index + dependency graph — `index.py`, `schema.sql`, delta refresh via Tooling-API `LastModifiedDate` diff (`delta.py`)
  3. Bundled knowledge base — `knowledge/store.py` + ~32 hand-authored Markdown entries under `knowledge/entries/{anti_patterns,best_practices,governor_limits,patterns}/`
  4. Project memory — `memory/store.py`, vector-recalled with decay
  - **`parsers/`** — one file per metadata type (ApexClass, ApexTrigger, CustomObject/Field, ValidationRule, RecordType, Flow, LightningComponentBundle). Two-pass ingestion: components first, then relationships. Adding a type = one new file + one import line, no DDL.
- **`memory/` — three tiers**: project memory (`store.py`, durable, per-`(tenant, org)` scope), working memory (`working.py`, task state machine + transcript in SQLite, powers `resume`), learning memory (`promote.py`, curated project-memory → knowledge-base promotion, heuristic-blocked on tenant-specific content unless `--force`). `extraction.py` runs the end-of-session `/quit` memory nudge.
- **`models/schemas.py`** — Pydantic models: `Task`, `TaskStatus`, `ExecutionPlan`, `PlanStep`, `RiskLevel`, `OrgConnection`, `AgentMode`, `ToolDefinition`, `PreflightCheck`. Shared vocabulary across every layer.
- **`prompts/`** — `load_system_prompt()` renders `system_prompt.md`, substituting `{{INDEX_FRESHNESS}}` so the LLM self-detects a stale index mid-task (`index_freshness.py`).

### Offline / test strategy

`--mock-org` stubs only the `sf` CLI (and remote-embedding) calls listed in `_SF_TOOLS`; the LLM is still live. Tests stub at the provider boundary and/or `drive_approval_loop`. The default `pytest` run touches no network and no org.
