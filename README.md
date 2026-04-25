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
uv run python -m sf_dev_agent setup         # interactive wizard
uv run python -m sf_dev_agent "List all Apex classes in the org"
```

That's the whole flow. The wizard handles everything else.

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

Verify each:

```bash
python --version    # 3.12+
uv --version
sf --version        # 2.100+
```

> **No Salesforce org yet?** Sign up free at [developer.salesforce.com/signup](https://developer.salesforce.com/signup) — pick "Developer Edition." Free forever, real org.

---

## The setup wizard

```bash
uv run python -m sf_dev_agent setup
```

The wizard will:

1. Verify `sf` CLI is installed
2. Show your connected orgs in a numbered menu (or run `sf org login web` if you have none)
3. Let you pick an LLM provider — links you to the exact key page
4. Validate the key with a one-token test call (fail-fast on bad keys)
5. Write a minimal `.env`

After it finishes, you're done — go straight to "Running the agent."

---

## Running the agent

```bash
# One-shot
uv run python -m sf_dev_agent "Create an Account trigger that prevents duplicate Phone numbers"

# Interactive REPL
uv run python -m sf_dev_agent

# Override the provider for a single run
uv run python -m sf_dev_agent --provider openai "..."

# Test the loop without touching your org or burning LLM tokens
uv run python -m sf_dev_agent --mock-org "Create a trigger"
```

When you ask for something that creates/modifies metadata, the agent:

1. Runs **preflight queries** (existing triggers, flows, validation rules on the target object).
2. Submits an **execution plan** with steps, risk level, rollback strategy, and impact counts.
3. Pauses at: `Approve this plan? [yes/no/modify]`
4. On `yes`, writes files to `workspace/force-app/main/default/` and runs `sf project deploy start` against your org with the test class.

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
│   ├── __main__.py            # CLI entry point + setup dispatcher
│   ├── setup_wizard.py        # interactive setup flow
│   ├── agent.py               # ReAct loop, plan-approve-execute state machine
│   ├── paths.py               # repo_root() / agent_workspace() helpers
│   ├── sf_config.py           # auto-derive org type / instance URL / API version
│   ├── providers/             # anthropic / openai / gemini adapters
│   ├── tools/registry.py      # sf CLI wrappers, file I/O, bash
│   ├── prompts/               # system prompt template
│   └── models/schemas.py      # Pydantic models (Task, ExecutionPlan, ...)
├── workspace/                 # SFDX project — agent reads/writes metadata here
├── tests/                     # pytest suite
├── docs/                      # design notes, project summary
└── .env                       # your config (gitignored)
```

---

## Safety model

- **Read-only tools** (`sf_metadata_describe`, `sf_soql_query`, `sf_retrieve`, `code_search`, `file_read`) run freely during planning.
- **Write tools** (`file_write`, `sf_source_deploy`, `sf_apex_execute`, `bash`) are **blocked during Phase 1** — they fail with a clear error if the LLM tries to call them before approval.
- **Approval is required** before any write can execute. There is no `--yes` flag.
- **Mock-org mode** (`--mock-org`) stubs all `sf` CLI calls but the LLM is still real — useful for testing prompts and the plan flow without touching your org.

---

## Roadmap

See [docs/PROJECT_SUMMARY.md](docs/PROJECT_SUMMARY.md) for the multi-week build plan covering metadata indexing, semantic code search, an approval UI, and project memory.
