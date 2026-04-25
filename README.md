# Salesforce Developer Agent

An AI-powered CLI agent that plans, approves, and executes Salesforce development tasks against a real org — write Apex triggers, classes, flows, and validation rules from natural-language prompts.

The agent runs a **plan → approve → execute** loop:

1. **Phase 1 — Planning.** It inspects the org (read-only), drafts a structured plan with risk + rollback, and submits it for approval.
2. **Approval gate.** You review the plan and type `yes` / `no` / `modify`.
3. **Phase 2 — Execution.** Only after approval does it write files and deploy to the org.

It is **provider-agnostic** — works with Anthropic Claude, OpenAI GPT, or Google Gemini.

---

## Prerequisites

You need all four of these installed and ready before the agent will run.

| Requirement | Why | How to get it |
|---|---|---|
| **Python 3.12+** | Runs the agent | [python.org/downloads](https://www.python.org/downloads/) |
| **uv** | Python package + venv manager | `pip install uv` or [docs.astral.sh/uv](https://docs.astral.sh/uv/) |
| **Salesforce CLI (`sf`)** | Talks to your org | `npm install -g @salesforce/cli` (Node 18+) |
| **An LLM API key** | The agent's brain | One of: Anthropic, OpenAI, or Google Gemini (free tier works) |

Verify each works:

```bash
python --version    # 3.12 or higher
uv --version
sf --version        # 2.100+ recommended
node --version      # 18+; sf CLI needs it
```

---

## Step-by-step setup

### 1. Clone and install dependencies

```bash
git clone <this-repo>
cd sf-dev-agent
uv sync                                  # installs base dependencies
uv pip install -e '.[gemini]'            # add your provider: gemini | openai | anthropic | all
```

### 2. Authenticate to your Salesforce org

You need at least one connected org. The agent will use it via the `sf` CLI.

```bash
sf org login web --alias AgentforceOrg --instance-url https://login.salesforce.com
```

This opens a browser, you log in, and `sf` stores the auth token under the alias `AgentforceOrg`. Verify:

```bash
sf org list
```

You should see your alias with status `Connected`.

> **No org yet?** Sign up free at [developer.salesforce.com/signup](https://developer.salesforce.com/signup) — pick "Developer Edition." It's free forever and gives you a real org to deploy to.

### 3. Get an LLM API key

Pick **one** provider — you only need one key.

| Provider | Where to get a key | Free tier? |
|---|---|---|
| **Google Gemini** | [aistudio.google.com](https://aistudio.google.com) → "Get API key" | Yes — 250 req/day for `gemini-2.5-flash` |
| **OpenAI** | [platform.openai.com/api-keys](https://platform.openai.com/api-keys) | No — pay-as-you-go |
| **Anthropic** | [console.anthropic.com](https://console.anthropic.com) | No — pay-as-you-go |

> **Gemini free-tier note:** the daily quota is shared *per Google AI Studio project, not per key*. If you exhaust it, rotating the key in the same project won't help — create a new project or enable billing.

### 4. Configure your environment

Copy the example file and fill in your values:

```bash
cp .env.example .env
```

Edit `.env`:

```ini
# Pick one provider
LLM_PROVIDER=gemini
GOOGLE_API_KEY=AIzaSy...your-key-here

# Your Salesforce org (must match the alias from `sf org list`)
SF_ORG_ALIAS=AgentforceOrg
SF_ORG_TYPE=developer                    # or: sandbox | scratch | production
SF_INSTANCE_URL=https://orgfarm-XXXXX-dev-ed.develop.my.salesforce.com
SF_API_VERSION=62.0

# Where the agent writes files (use absolute path)
AGENT_WORKSPACE=C:/Users/you/projects/sf-dev-agent/workspace
```

> **Tip:** get your real `SF_INSTANCE_URL` from `sf org display --target-org AgentforceOrg --json` — copy the `instanceUrl` field.

### 5. Verify the workspace is set up

The repo ships with a pre-configured `workspace/` directory containing `sfdx-project.json` and `config/project-scratch-def.json`. The agent reads/writes Salesforce metadata there.

If it's missing, recreate it:

```
workspace/
├── sfdx-project.json
├── config/
│   └── project-scratch-def.json
└── force-app/main/default/
    ├── classes/
    ├── triggers/
    └── flows/
```

### 6. First run — read-only sanity check

Start with a question that doesn't change anything:

```bash
uv run python -m sf_dev_agent --provider gemini "List all Apex classes in the org and tell me what they do"
```

You should see:
- Banner showing your org alias and provider
- `Phase 1: Planning` with one or more `Tool call: sf_metadata_describe` lines
- A natural-language summary of the classes the agent found

If this works, the wiring is correct.

### 7. Full flow — plan, approve, deploy

Now try a real task:

```bash
uv run python -m sf_dev_agent --provider gemini "Create a before-insert/before-update Apex trigger on Account that prevents duplicate Accounts based on the Phone field. Include a test class with at least 2 methods and 90% coverage."
```

The agent will:

1. Run preflight checks (looks for existing triggers/flows/validation rules on Account).
2. Print an **Execution Plan** panel with steps, risk level, rollback strategy, and impact counts.
3. Prompt: `Approve this plan? [yes/no/modify]`
   - `yes` — moves to Phase 2, writes the files, deploys, runs tests
   - `no` — cancels the task
   - `modify` — describe what to change, the agent re-plans
4. On approval, you'll see file writes, a `sf project deploy start`, and test results.

Once deployed, verify in your org:

```bash
sf org open --target-org AgentforceOrg --path /lightning/setup/ApexTriggers/home
```

---

## Command reference

```bash
# Required: pick a provider (or set LLM_PROVIDER in .env)
--provider {anthropic|openai|gemini}

# Optional: override the default model for that provider
--model gemini-2.5-pro

# Optional: override the org from .env
--org-alias MyScratch
--org-type scratch
--instance-url https://test.salesforce.com
--api-version 62.0

# Test the agent loop without hitting Salesforce CLI (canned responses)
--mock-org

# Debug logging
--verbose
```

**Interactive mode** — omit the prompt to chat back-and-forth:

```bash
uv run python -m sf_dev_agent --provider gemini
sf-agent: Show me all the Account validation rules
...
sf-agent: Now add a new one that requires Industry to be set
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `FileNotFoundError: [WinError 2]` calling `sf` | The agent uses `sf.cmd` on Windows — make sure `npm install -g @salesforce/cli` succeeded and `where sf.cmd` returns a path. |
| `429 RESOURCE_EXHAUSTED` from Gemini | Daily free-tier quota (20–250 req/day depending on model) hit. Create a new Google AI Studio project, or enable billing, or wait for the daily reset (midnight Pacific). |
| `InvalidProjectWorkspaceError` from sf | The workspace path in `.env` (`AGENT_WORKSPACE`) doesn't contain a valid `sfdx-project.json`. Verify the path and the file. |
| `NotADevHubError` when creating scratch orgs | Your target org isn't a Dev Hub. Either use a Developer Edition org directly (set `SF_ORG_TYPE=developer`) or enable Dev Hub in Setup → Dev Hub. |
| Plan never appears, agent just answers | The model decided no plan was needed (read-only question). For write operations, the system prompt requires `submit_plan` — re-phrase to be explicit about creating/modifying. |
| `Provider not installed` | Run `uv pip install -e '.[gemini]'` (or `[openai]` / `[anthropic]` / `[all]`) for your chosen provider. |

---

## Project layout

```
sf-dev-agent/
├── src/sf_dev_agent/
│   ├── __main__.py            # CLI entry point
│   ├── agent.py               # ReAct loop, plan-approve-execute state machine
│   ├── providers/             # anthropic / openai / gemini adapters
│   ├── tools/registry.py      # sf CLI wrappers, file I/O, bash
│   ├── prompts/               # system prompt template
│   └── models/schemas.py      # Pydantic models (Task, ExecutionPlan, ...)
├── workspace/                 # SFDX project — agent writes metadata here
├── tests/                     # pytest suite
├── docs/                      # design notes, project summary
└── .env                       # your config (gitignored)
```

---

## Safety model

- **Read-only tools** (`sf_metadata_describe`, `sf_soql_query`, `sf_retrieve`, `code_search`, `file_read`) run freely during planning.
- **Write tools** (`file_write`, `sf_source_deploy`, `sf_apex_execute`, `bash`) are **blocked during Phase 1** — they fail with a clear error if the LLM tries to call them before approval.
- **Approval is required** before any write can execute. There is no `--yes` flag.
- **Mock-org mode** (`--mock-org`) stubs all `sf` CLI calls — useful for testing prompts and the plan flow without touching your org.

---

## Roadmap

See [docs/PROJECT_SUMMARY.md](docs/PROJECT_SUMMARY.md) for the multi-week build plan covering metadata indexing, semantic code search, an approval UI, and project memory.
