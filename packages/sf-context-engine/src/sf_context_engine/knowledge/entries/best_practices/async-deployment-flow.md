---
id: bp-async-deployment-flow
title: Sandbox-first deployment with validate-only checks
category: best_practice
severity: critical
tags: [deployment, sandbox, ci, production, change_management]
references:
  - https://developer.salesforce.com/docs/atlas.en-us.sf_cli_v2.meta/sf_cli/cli_reference_project_commands_unified.htm
---

Production deploys to Salesforce should never be the first time the change is tested in a Salesforce-shaped environment. The non-negotiable sequence:

**1. Scratch org / dev sandbox** — first integration test.
**2. Full or partial sandbox** — production-shaped data volumes; full RunLocalTests.
**3. UAT sandbox** — admin/business sign-off.
**4. Validate-only deploy to production** — `--dry-run` against real prod metadata to surface conflicts.
**5. Real deploy to production** — promotion of the validated package.

**`sf project deploy` flags that matter:**
- `--dry-run` (validate-only) — runs the deploy and tests against prod without committing. The validation ID can be used for a Quick Deploy within ~10 days, skipping the test re-run.
- `--test-level RunLocalTests` — required for prod deploys; runs every non-managed-package test in the org.
- `--test-level RunSpecifiedTests --tests Class1,Class2` — for sandbox iterations where you want to run only the relevant test classes.

**Deploy windows.** Schedule production deploys outside business hours and during a low-async-job window — running tests during business hours can knock out the daily 250k async job ceiling.

**Rollback plan.** Before deploying, write down (a) which components are changing, (b) how to undo each (delete? redeploy old? re-enable a config flag?), and (c) the validation ID. Rollback at 2 AM is not the time to figure this out.

Always commit the agent's plan-document for non-trivial deploys to a "deployment log" custom object or a git-tracked file so post-incident review is possible.
