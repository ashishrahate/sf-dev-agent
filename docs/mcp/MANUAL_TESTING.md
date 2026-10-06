# Manual testing: Cursor and VS Code (GitHub Copilot)

The Claude Code runs are automated (`scripts/mcp_bench.py`, results in `RESULTS.md`). Cursor and Copilot are interactive, so this is the procedure for running the **same tasks by hand** and recording comparable results. Allow about 2 hours per client for the full matrix (6 tasks x 3 configs, one run each), or 30 minutes for the quick pass at the end.

Menu names below match recent versions but move around; if something isn't where it says, search the client's settings for "MCP".

## 0. What you are testing

| Config | sf-context | Salesforce DX MCP |
|---|---|---|
| **A** | off | on |
| **B** | on | off |
| **C** | on | on |

Two settings (the automated runs used both):

- **repo**: open a folder that contains the org's source as a DX project (see 1.4). The agent can read and edit files.
- **org**: open an **empty** folder. The agent has no local source and must learn about the org through MCP only. This is where sf-context showed its biggest advantage.

## 1. One-time setup (both clients)

### 1.1 Prerequisites

| Need | Check |
|---|---|
| Salesforce CLI | `sf --version` |
| An authenticated **sandbox or developer** org alias | `sf org list` (T2 and T6 write to the org; never use production) |
| Node 20+ and `npx` | `node --version` (launches `@salesforce/mcp`) |
| `uv` | `uv --version` |
| This repo checked out on `feature/context-mcp-server` | |

### 1.2 Install and warm up

```bash
cd <repo>/sf-dev-agent
uv sync --all-packages --all-extras      # MCP SDK + local embedder
uv run sf-context-mcp --warmup           # one-time ~67 MB model download
```

### 1.3 Build the index (once per org)

The index lives in `~/.sf-context/` and is read-only for the tests below except T4 and T6 (see section 4). Build it with the repo's preflight and a one-off script, or let an agent do it with the `build_metadata_index` tool in config B/C. The script route:

```bash
uv run python - <<'PY'
from sf_context_engine.service import ContextService
s = ContextService(org_alias="<alias>")
print(s.build_metadata_index(full_refresh=True)["changes"]["counts"])
print(s.embed_metadata_index()["embedded"], "embedded")
print(s.embed_knowledge_base()["embedded"], "knowledge entries embedded")
PY
uv run python scripts/mcp_preflight.py --org <alias>     # everything should say OK
```

Takes about 2 to 3 minutes for a small org. Expect no `MISSING` lines from the preflight.

### 1.4 Local copy of the org (for the `repo` setting)

```bash
mkdir bench-repo && cd bench-repo
mkdir -p force-app/main/default
cat > sfdx-project.json <<'EOF'
{ "packageDirectories": [{ "path": "force-app", "default": true }], "sourceApiVersion": "67.0" }
EOF
sf project retrieve start --target-org <alias> -m ApexClass -m ApexTrigger -m LightningComponentBundle -m Flow -m CustomObject
```

Keep an untouched copy (`cp -r bench-repo bench-repo-baseline`). You will need it to restore the org after T2 and T6, and to start each `repo` run from a clean copy.

### 1.5 Put the MCP config in place

Config files are in `docs/mcp/configs/`. **Edit the alias and the repo path first.** On Windows the DX server is started through `cmd /c npx`; on macOS/Linux change that entry to `"command": "npx"` and drop the `/c`.

| Client | Copy to | Notes |
|---|---|---|
| Cursor | `.cursor/mcp.json` in the opened folder (or `~/.cursor/mcp.json` for global) | Then open Cursor Settings, MCP (called "Tools & MCP" in recent versions), and switch each server on. A green dot means it started. |
| VS Code | `.vscode/mcp.json` in the opened folder | VS Code shows "Start" above each server in the file; or run the command **MCP: List Servers**. Use **Copilot Chat in Agent mode** (not Ask/Edit). Trust the server when prompted. |

If a server shows an error, open its output/logs from that same panel. The usual causes: wrong repo path, `uv` or `npx` not on the PATH that the editor sees, or the org alias not authenticated.

### 1.6 Switching configurations

Never test with an extra server enabled by accident. For each run:

| Config | Cursor / VS Code |
|---|---|
| A | enable `salesforce-dx`, **disable** `sf-context` |
| B | enable `sf-context`, **disable** `salesforce-dx` |
| C | enable both |

Then open the tools list in the chat UI and confirm which tools are present before you send the prompt (sf-context has 12 tools starting with `retrieve_context`; DX has 8 such as `run_soql_query` and `deploy_metadata`). In Copilot you can also untick tools in the tools picker.

### 1.7 Fixed conditions (write them down once)

- Same model for every run in a client (record its exact name). Do not use "Auto" model selection.
- Fresh chat for every run (T4b needs a **new** chat after T4a).
- Approve MCP tool calls as they come; do not give extra hints or follow-ups. One prompt, then wait for the final answer.
- If the client offers local file/terminal tools, leave them on in `repo` runs and **off** (or open an empty folder) in `org` runs.

## 2. The tasks

Paste the prompt exactly. Answer keys below are for `AgentforceOrg` as tested; if you use another org, derive keys with `dependency_graph` and the org itself before running.

| # | Prompt | Config | What a correct answer contains |
|---|---|---|---|
| **T1** | List everything in this org that touches the Account object: triggers, flows, validation rules and the classes involved, and how they depend on each other. | A B C | `AccountTrigger` (before insert/update) calls `AccountTriggerHandler` calls `AccountService.preventDuplicateAccounts` calls `AccountSelector.selectByPhoneNumbers`; test classes `AccountTriggerHandler_Test`, `AccountService_Test`, `AccountSelector_Test`, `AccountTrigger_Test`; flow `Append_Billing_Country_to_Account_Name` (record-triggered, before save). **The flow is Active (v8) with a newer Draft v9: saying it "isn't running" is wrong.** No validation rules exist. |
| **T2** | Add validation to AccountTriggerHandler so it rejects an Account with negative AnnualRevenue. Don't break existing callers, follow this org's trigger conventions, include a test class, and deploy it to the org. | A B C | Logic added through the existing trigger, handler and service chain (no second trigger), bulk-safe, `addError` on `AnnualRevenue` when `< 0`, a test that covers it, deployed (A and C only; B has no deploy tool, so grade B on code). **Writes to the org: see 3.** |
| **T3** | Review AccountTriggerHandler against Salesforce best practices. Which governor-limit or anti-pattern problems apply? | A B C | Handler is a thin delegator; SOQL is isolated in `AccountSelector` (bulk, `WITH SECURITY_ENFORCED`); the static `bypassTrigger` flag is mentioned; **no** SOQL/DML-in-loop problem is reported. Inventing a limit violation is a wrong claim. |
| **T4a** | Record this decision for the team: we prefer Apex triggers with a handler class over Flows for complex Account logic, because the team owns the Apex skills. | B C | The agent saves it via `memory_save` and confirms. (A has no persistent store; skip.) |
| **T4b** | *(new chat)* I need to add logic on Account insert. How should I do it here? | B C | Recommends the handler/service route **and cites the saved decision unprompted**. |
| **T5** | Which classes call RecordSelectorController, and what would break if I rename it? | A B C | `recordSelector` LWC (imports the Apex method) and `RecordSelectorControllerTest`; the LWC import and the test would break; dynamic string references are not visible to any tool. |
| **T6** | Is your view of the org up to date? Refresh it if not, then tell me what changed. | B C | **Requires 4.2 first.** The agent should call `build_metadata_index` itself and name `ApexClass:AccountSelector` as modified. |

## 3. Safety: T2 writes to the org

T2 (configs A and C) deploys code. Run it only against a sandbox or developer org. After **each** T2 run, restore the org and verify:

```bash
cd bench-repo-baseline
sf project deploy start --target-org <alias> --ignore-conflicts \
  --source-dir force-app/main/default/classes --source-dir force-app/main/default/triggers
```

If the agent added **new** classes or triggers, delete them:

```bash
sf project delete source --target-org <alias> --no-prompt -m ApexClass:<NewClassName>
```

Verify nothing differs from the baseline: compare each class body with `sf data query --use-tooling-api -q "SELECT Name, Body FROM ApexClass WHERE NamespacePrefix = null"`, or simply retrieve again into a temp folder and `diff -r` it against `bench-repo-baseline`.

For `repo` runs also reset the local copy before the next run: `rm -rf bench-repo && cp -r bench-repo-baseline bench-repo`.

## 4. Special procedures

### 4.1 T4 (memory) isolation

T4a writes to the shared index database, and T4b reads it. Use a throwaway database for the pair and delete it afterwards:

1. Copy the index: `cp ~/.sf-context/metadata_index.db ~/t4.db`.
2. In the MCP config, add to the `sf-context` entry: `"env": { "SF_CONTEXT_DB": "<full path to>/t4.db" }`, and restart the server.
3. Run T4a. Open a **new chat**. Run T4b.
4. Remove the `env` line (or point it at a fresh copy) before any other task, so the saved memory does not leak into other runs.

### 4.2 T6 (staleness) setup

Make a harmless change in the org that the index has not seen, immediately before the run:

```bash
cd bench-repo-baseline     # a DX project containing force-app/.../classes/AccountSelector.cls
echo "// manual-test marker" >> force-app/main/default/classes/AccountSelector.cls
sf project deploy start --target-org <alias> --ignore-conflicts \
  --source-dir force-app/main/default/classes/AccountSelector.cls
```

Run T6 (B, then C; redo 4.2 between them or use a throwaway DB copy as in 4.1 so the second run also sees a stale index). Afterwards restore the class (the redeploy in section 3, then remove the marker line from your baseline copy).

## 5. What to record

One row per run, in the table at the bottom of `RESULTS.md` (or a copy of it):

| Field | How to get it |
|---|---|
| Client, model | exact names |
| Task, setting, config | e.g. T1, org, C |
| Correct (0-3) | 0 wrong, 1 partial, 2 correct, 3 correct and noticed something extra. Grade against the answer key in section 2. Mark **wrong claims** separately. |
| Tool calls | count, split sf-context / DX / editor tools. Cursor and Copilot both show each tool call in the chat. |
| Wrong claims | count of statements the org contradicts (the T1 flow status is the known trap) |
| Unprompted use | did the agent reach for sf-context without being told? y/n |
| Tokens / cost | only if the client shows it; otherwise write `n/a`. Do not compare token counts across different clients. |
| Seconds | prompt sent to final answer |
| Notes | server errors, timeouts, wrong tool choice, approval prompts, mock-embedder warnings |

Repeat each cell 3 times if you have the time; with one run per cell, differences of a few seconds or tool calls are noise.

## 6. Quick pass (30 minutes)

If you only have half an hour per client, run these seven cells, all in the **org** setting:

1. T1 in A, then B, then C
2. T5 in A, then C
3. T4a then T4b in C

This is the comparison where the automated runs showed the clearest difference (DX alone needed roughly twice the tool calls and tool-result tokens for read-only questions).

## 7. Troubleshooting

| Symptom | Likely cause |
|---|---|
| Server never starts | wrong path in the config; `uv` / `npx` not on the editor's PATH (launch the editor from a terminal to inherit it); alias not authenticated |
| `sf-context` tools say the index is missing | built under a different `SF_CONTEXT_HOME` or `SF_CONTEXT_DB`; rerun 1.3 with the same environment as the server |
| Results look random / semantic search is poor | `index_status` says `embedder_is_mock: true` (the local model is not installed: rerun 1.2) |
| `embedder_mismatch` error | the index was built with a different embedder; call `embed_metadata_index` with `reset_embeddings=true` |
| Build tool takes minutes | normal for the first build (about 1 to 3 minutes); the client may show a spinner with no output |
| Agent never uses sf-context in C | record it ("unprompted use: n"); that is a finding about the tool descriptions, not a setup error |
