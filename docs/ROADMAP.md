# Salesforce Developer Agent — Roadmap

Authored 2026-04-27 after Wave 8 (memory tiers) shipped end-to-end. This document is the canonical backlog: every gap from the architecture description in `PROJECT_SUMMARY.md` plus a concrete implementation plan for the user-journey friction points called out at the end of Wave 8.

The goal is a tracked artifact, not memory. When you pick up work next session, this is the place to start.

---

## Part 1 — Architecture gaps by plane

Status as of 2026-04-27. Roughly: agent plane ~95% done, data plane ~70% (inherited via sf CLI), control plane ~20%, execution plane ~5%.

### Agent plane — *near done*

| Gap | Priority | Effort | Notes |
|---|---|---|---|
| Anthropic Claude Agent SDK migration | Nice-to-have | 1-2 weeks | Currently using the direct `anthropic` SDK. Migration unlocks managed loop primitives but no functional change. |
| Rollback engine | Must-have for prod | 1 week | Every plan has `rollback_strategy`; nothing executes it on Phase 2 failure. Needed before write tasks against production orgs. |
| Real Apex AST parser | Nice-to-have | 1-2 weeks | Today: regex extraction of class refs. Real AST would catch more relationships and survive obscure formatting. |
| Per-method Apex chunking | Nice-to-have | 1 week | Needs usage signal first — current class-level chunking may already be sufficient. |
| ValidationRule / RecordType / ListView parsers | Nice-to-have | 2-3 days each | Same generic-parser story as ApexClass / CustomObject. Schema doesn't change. |
| Standard-object delta refresh | Nice-to-have | 3 days | Needs an EntityDefinition pivot in the Tooling-API query. |
| Real-org pressure test of the 4-layer orchestrator | Must-have for prod | 2-3 days | Held until org access. Wave 8 added memory as the 4th source; needs validation against a real org with thousands of components. |
| Drop the `build_metadata_index` mock-mode shim | Maintenance | 2 hours | Tracked in `memory/todo_index_shim.md`. Either fail-loudly in mock or ship a fixture-populated DB. |

### Data plane — *functional via sf CLI inheritance*

| Gap | Priority | Effort | Notes |
|---|---|---|---|
| Own OAuth Connected App + Web Server flow | Must-have for prod | 2-3 weeks | Currently we ride sf CLI's connected app. Owning ours decouples auth and lets us pin scopes per task. |
| Per-tenant token vault | Must-have for prod | 1-2 weeks | python-dotenv today; AWS Secrets Manager / Vault for prod. |
| Network egress lockdown to client's instance URL | Must-have for prod | 3-5 days | Depends on execution-plane containers; without containers there's no enforcement layer. |
| Refresh token encryption at rest (AES-256) | Must-have for prod | 1 week | Currently sf CLI's keychain handles this; we have no control. |

### Control plane — *mostly not built (~20%)*

This is the biggest missing surface. The agent works for an individual developer; productizing for clients lives here.

| Gap | Priority | Effort | Notes |
|---|---|---|---|
| Real authentication + tenant lifecycle | Must-have for prod | 3-4 weeks | `tenant_id="local-dev"` is hardcoded in `__main__.py:133`. Need creation/suspension/scoping of real tenants. |
| Orchestration API (task submission, queue, dispatch) | Must-have for prod | 4-6 weeks | Today: CLI process per invocation. Need an HTTP API that accepts tasks, routes per-tenant, dispatches to agent workers. |
| Structured audit trail | Must-have for prod | 1-2 weeks | Slice 2a's `tasks` + `conversation_messages` get most task-level audit. Tool-level structured audit (tenant_id, task_id, tool_name, scrubbed-PII params, outcome, duration) is missing. |
| Configurable gating per client | Must-have for prod | 1 week | Currently hardcoded `READ_ONLY_TOOLS` / `WRITE_TOOLS` frozensets. Need a policy engine — some clients want hard gates on everything; power users may loosen for sandbox. |
| Billing / metering | Must-have for prod | 2-3 weeks | Zero today. Needs the audit pipeline first. |
| Secrets vault integration | Must-have for prod | 1 week | Wraps the data-plane token vault item. Same scope. |

### Execution plane — *not built (~5%)*

Currently the agent runs in the user's local Python environment. Every isolation guarantee in the architecture description is on the gap side.

| Gap | Priority | Effort | Notes |
|---|---|---|---|
| Container runtime (Docker → Firecracker/gVisor) | Must-have for prod | 3-4 weeks | Tool wrapper abstraction is in place — swappable without touching agent code. |
| Per-session ephemeral container | Must-have for prod | included in above | Tied to runtime choice. |
| Network egress restrictions | Must-have for prod | 1 week | Only enforceable inside a container with iptables / network namespaces. |
| In-memory credentials destroyed on terminate | Must-have for prod | included in above | Tied to container lifecycle. |

### Cross-cutting UX gaps

These are the ones we plan to close in **Part 2**.

| Gap | Priority | Effort | Notes |
|---|---|---|---|
| First-time prereq check + assisted install | High UX | 0.5 days | "Did you forget to install the sf CLI" support. |
| Auto-warm context engine on first use | High UX | 1 day | Today: index is empty until the agent figures out to call `build_metadata_index`. |
| Index staleness check + agent awareness | High UX | 0.5 days | Inject freshness into the system prompt; expose a tool. |
| `sf-agent resume <task-id>` CLI verb | Quick win | 0.5 days | Capability exists (`AgentLoop.resume`); only the CLI plumbing is missing. |
| End-of-session extraction nudge | Quick win | 0.5 days | Today: user must remember to run `memory extract` themselves. |
| Persistent terminal REPL with slash commands | High UX | 3-5 days | Replaces the basic `Prompt.ask` REPL with a `prompt_toolkit`-based experience. The unified UI surface. |
| Web/desktop UI | Out of scope (CLI-first) | — | Replaced by the persistent REPL. |
| CI integration / unattended mode | Out of scope (safety model) | — | Mandatory approval gate is by design. CI integration would need signed plans + policy engine, not a `--yes` flag. |

---

## Part 2 — Implementation plan for UX gaps

Six concrete deliverables. Each is independently shippable; later ones layer on top of the earlier ones cleanly.

---

### Item 2.1 — `sf-agent doctor` (system prereq check + optional install)

Stronger than `requirements.txt` because system tools (Node, sf CLI, Python itself) aren't pip-installable.

**Two-mode design:**
- `sf-agent doctor` — pure check. Probes each tool, reports green/red, prints the exact install command for any red on the user's OS.
- `sf-agent doctor --install` — best-effort install via detected package manager (`winget` on Windows, `brew` on macOS, `apt` on Debian/Ubuntu). Prints the command for things outside our reach (Python itself).

**Implementation surface:**
- New module `src/sf_dev_agent/doctor.py`. Hardcoded `PREREQUISITES` list of `(name, probe_command, min_version, install_commands_per_os)`.
- Probes: `python --version`, `uv --version`, `node --version`, `sf --version`, `git --version`, plus a `.env`-loaded LLM API key check.
- Output: `rich` table — name / status / version / install command if missing.
- Wired into `__main__.py` like `setup` and `memory` (special-case dispatch).
- The setup wizard calls `doctor` first; refuses to proceed if any required check fails.
- New tests in `tests/test_doctor.py` — probe individual tools with mocked subprocess output, verify the table renders, verify the wizard refuses on red.

**Scope**: ~150 LOC + tests. Half a day.

**Risk**: low. Pure additive; no existing flow changes except the wizard, which gains a new pre-check.

**Open question**: `--install` flag attempts installs (riskier, OS-specific) or prints only? **Recommendation**: print-only in v1, add `--install` as a v2 flag once we have telemetry on what users actually need.

---

### Item 2.2 — `sf-agent resume <task-id>` CLI verb

Smallest item on the list. `AgentLoop.resume()` is fully built; only the CLI plumbing is missing.

**Implementation surface:**
- Special-case `resume` in `__main__.py` (mirrors `setup`, `memory`).
- New `src/sf_dev_agent/resume_cli.py` module with argparse:
  - `sf-agent resume <task-id>` — resume that task.
  - `sf-agent resume --list` — list in-flight tasks (status not in terminal set) under the current scope.
  - `sf-agent resume --latest` — convenience: resume the most-recent in-flight task.
- Inside, instantiates `OrgConnection`, provider, `WorkingMemoryStore`, calls `AgentLoop.resume(...)`.
- Tests in `tests/test_resume_cli.py` — resume scripted task via fake provider, verify state transitions, verify `--list` returns in-flight only, verify `--latest` resolves correctly.

**Scope**: ~80 LOC + tests. Half a day.

**Risk**: trivial. Capability already validated by 8 existing tests in `test_working_memory.py`.

---

### Item 2.3 — Auto-warm context engine + staleness check

Two layers, both small.

**Layer A — Auto-warm on first use:**
- New helper module `src/sf_dev_agent/index_freshness.py` with an `IndexFreshness` dataclass: `(last_built_at, age_seconds, is_stale, embedding_coverage_percent)`.
- Function `check_index_freshness(db_path, org_alias)` queries the existing `index_runs` table + `components` embedding column.
- On agent startup (CLI one-shot or REPL launch): if `last_built_at is None`, prompt:
  > First run against `<org_alias>`. The context engine needs to build a metadata index (~30s) and embed it (~1 min). Run now? [yes / skip / no-and-stop-asking]
- On `yes`: run `build_metadata_index` → `embed_metadata_index` → `embed_knowledge_base` with `rich` progress bars.
- On `skip`: continue but warn that retrieval will be empty.
- On `no-and-stop-asking`: write a flag to `.cache/.skip_warmup` so we don't ask again.

**Layer B — Staleness check on every task:**
- Threshold: `is_stale = age_seconds > 24h` (configurable via `INDEX_STALE_AFTER_HOURS` env).
- If stale, inject a one-line note into the agent's system prompt at runtime: `[index freshness: last built 31h ago — call build_metadata_index --delta if you suspect stale data]`.
- Expose `check_index_freshness` as a tool so the agent can self-check during planning.

**Implementation surface:**
- `src/sf_dev_agent/index_freshness.py` — pure SQLite queries. ~80 LOC.
- Hook into `__main__.py` startup and the future REPL launch.
- New `check_index_freshness` tool registered in `tools/registry.py`.
- System prompt template gets a `{INDEX_FRESHNESS}` placeholder filled at render time.
- Tests in `tests/test_index_freshness.py` — empty index → `last_built_at is None`; recent run → `is_stale=False`; backdated run → `is_stale=True`; embedding-coverage math.

**Scope**: 1 day. ~150 LOC + tests.

**Risk**: low. Touches startup path; needs careful TTY check so scripted runs don't see the prompt.

**Open question**: the first-run warm-up prompt — soft (yes/no) or hard (auto-warm, no prompt)? **Recommendation**: soft. The user might want to skip on a quick read-only question.

---

### Item 2.4 — End-of-session extraction nudge

Two trigger points, both fire only when stdin is a TTY (don't pollute scripted/CI runs).

**A. Per-task nudge** (after each `agent.run()` reaches a terminal status):
- New method `AgentLoop._post_task_nudge()`. Called from the end of `run()`.
- Prompts: `Task complete. Extract memories from this conversation? [yes / skip / never-ask-again]`
- On `yes`: invokes `MemoryExtractor` inline, walks each candidate, persists accepted ones.
- On `never-ask-again`: writes `EXTRACT_NUDGE=off` to `.env`.

**B. REPL `/quit` nudge** (lands with the REPL in 2.5):
- Tracks tasks completed during this REPL session.
- On `/quit`: `You completed N tasks this session. Run extraction across all of them? [yes / skip]`
- On `yes`: walks each task's transcript through `MemoryExtractor` in turn.

**Implementation surface:**
- ~50 LOC in `agent.py` for the per-task variant.
- ~30 LOC in `repl.py` for the per-session variant (lands with 2.5).
- Suppression flag: `--no-extract-nudge` on the CLI, `EXTRACT_NUDGE=off` in `.env`.
- Tests in `tests/test_extract_nudge.py` — TTY check, `never-ask-again` writes the env flag, suppressed when `EXTRACT_NUDGE=off`, prompt invokes `MemoryExtractor` with the right task_id.

**Scope**: half a day for 2.4A, another quarter day for 2.4B alongside 2.5.

**Risk**: UX risk only — too-frequent nudges become annoying. The `never-ask-again` opt-out is essential. Default-on for new users; easy off-switch.

---

### Item 2.5 — Persistent terminal REPL with slash commands

Biggest single piece. Absorbs `/resume`, `/extract`, `/refresh-index`, `/status` as first-class affordances.

**Library**: `prompt_toolkit`. Industry standard for Python REPLs. Gives:
- Slash-command tab completion
- Multiline input (Shift+Enter)
- History (up-arrow recalls past prompts; persisted to `~/.sf-agent/history`)
- Status bar at the bottom (org / provider / current task / memory size / index freshness)
- Async-safe (so streaming LLM output works while the user types)

**v1 slash command set:**

| Command | What it does |
|---|---|
| `/help` | List commands |
| `/quit`, `/exit` | Leave the REPL (with extract-nudge if tasks completed this session) |
| `/clear` | Clear current task; keep all persistent state |
| `/status` | Org / provider / current task / memory count / index freshness |
| `/index` | Run `build_metadata_index --delta` + `embed_metadata_index` + `embed_knowledge_base` |
| `/resume <id>` | Pick up a persisted task (Wave 8 slice 2b machinery) |
| `/tasks [--in-flight]` | List recent tasks |
| `/memory recall <query>` | Quick recall without the agent loop |
| `/memory list [--type]` | Browse stored memories |
| `/memory extract` | Run extraction on the current task |
| `/memory export [--out]` | Dump memories to disk |
| `/memory promote --memory-id <id> --category <cat>` | Promote to knowledge entry |
| `/mock on\|off` | Toggle mock-org mode mid-session |
| `/provider <name>` | Switch LLM provider mid-session |
| `/verbose on\|off` | Toggle debug logging |

Free-form input (no leading `/`) goes to `agent.run()`.

**Status line**: bottom-of-terminal toolbar showing `org=AgentforceOrg | provider=gemini | task=task_xyz [planning] | mem=47 | index=2h ago`. Updates after each tool call and state transition.

**Streaming output**: assistant text streams as it's generated, not dumped after the full response. **Phased**: v1 ships non-streaming (matches today's behavior); add `chat_stream()` to provider abstractions in v1.1.

**Implementation surface:**
- `src/sf_dev_agent/repl.py` — owns the REPL loop and slash-command registry.
- `src/sf_dev_agent/repl_commands.py` — one function per slash command, registered into a dict.
- `__main__.py` — when invoked with no `request` argument and stdin is a TTY, launch the new REPL instead of the existing `Prompt.ask` loop.
- Add `prompt_toolkit>=3.0` to `pyproject.toml`.
- Tests in `tests/test_repl.py` — slash command parsing, status-line rendering, history persistence, free-form input dispatch to `agent.run()`.

**Scope**: 3-5 days. ~600-800 LOC.

**Risk**: medium. New dependency, new top-level UX. Phased delivery: ship without streaming or fancy completion in v1; expand over time.

**Tradeoff**: this is a real product investment. Worth doing if interactive use is the primary mode. The framing "instead of Web UI we can get this agent to run in the terminal" tells us the answer is yes — the REPL is the UI.

---

### Item 2.6 — Documentation refresh

Four small doc updates after the items above land:
- `README.md` — add a section for `sf-agent doctor`, the `memory` subcommand verbs (extract / export / promote), `sf-agent resume`, and a screenshot/transcript of the new REPL.
- `docs/PROJECT_SUMMARY.md` — sync the "Shipped" table with Wave 8's actual scope.
- `docs/ROADMAP.md` (this document) — close items as they ship.
- New `docs/USER_JOURNEY.md` — the end-to-end story we walked through, updated to reflect post-2.x friction reductions.

**Scope**: 1 day total, spread across the items.

---

## Recommended phasing

| Phase | Items | Effort | Why this order |
|---|---|---|---|
| **A — Document** | This doc | done | Sets the backlog so the rest fits in a tracked plan, not memory. |
| **B — Quick UX wins** | 2.1 doctor → 2.2 resume → 2.3 auto-warm + staleness → 2.4 extract nudge | 2-3 days | Each is small (≤1 day), each is independently shippable, and together they close most of the "first-time user friction" surface. |
| **C — REPL** | 2.5 prompt_toolkit-based REPL with v1 slash commands | 3-5 days | Bigger investment, but absorbs everything. Once it's there, `/resume`, `/extract`, `/index`, `/status` are first-class. |
| **D — Docs** | 2.6 docs refresh | 1 day | Lands after everything else so the docs reflect reality. |

**Why B-before-C**: each phase-B item makes the existing one-shot CLI markedly better on its own. The REPL is the bigger architectural commitment — landing the small wins first means if the REPL gets pushed, the user-facing experience still improves.

**Where the items overlap with the REPL**: phase B's `resume`, `extract-nudge`, and `auto-warm` migrate into the REPL as slash commands in phase C — but they're written as standalone CLI verbs first, then wrapped. No throwaway work.

---

## Out of scope for this roadmap

To keep the scope honest:

- **Streaming LLM output** in the REPL — flagged as "phase C+1" because every provider needs a `chat_stream()` method.
- **Tab completion of memory IDs** — nice but not essential for v1 REPL.
- **A `--yes` auto-approve flag** — explicitly out of scope per the safety model.
- **CI integration** — needs signed plans + a policy engine, not a flag. Separate roadmap item once the policy engine is on the table.
- **Containerization or auth** — control / execution plane work; tracked in Part 1 but not built in Part 2.
- **Web/desktop UI** — replaced by the persistent REPL.

---

## Tracking

Update this document as items land. The convention:

- ✅ in front of an item that's shipped + the commit hash.
- 🚧 for items in progress.
- ❌ for items that were planned but explicitly cancelled (with a reason).

Empty checkbox = not started.
