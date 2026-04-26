---
id: pt-flow-vs-apex-decision
title: When to use Flow vs Apex
category: pattern
severity: info
tags: [flow, apex, automation, architecture, low_code]
references:
  - https://architect.salesforce.com/decision-guides/trigger-automation
---

Salesforce's official guidance is "Flow first, Apex when needed." This is correct in spirit but requires nuance — Flow has specific cases where it's clearly the right tool and others where Apex is non-negotiable.

**Flow is right when:**
- The logic is admin-maintainable: simple field updates, record creation, sending emails, calling Approvals.
- The orchestration involves Salesforce-managed actions (Slack post, Quip update, MuleSoft, Einstein).
- The business owners need to see and edit the rules without involving developers.
- Cross-object record creation that doesn't require complex per-record computation.
- Scheduled paths and time-based triggers (much easier than Scheduled Apex + DML rules).

**Apex is right when:**
- Iteration over collections with O(n²) potential — Flow's loops are notorious for hitting governor limits faster than equivalent Apex.
- Anything calling out to a non-managed external system (Apex callouts give you full HttpRequest control).
- Complex transformation, parsing (JSON, XML, regex).
- Anything that needs to be unit-tested with assertions — Flow tests exist but are weaker than Apex tests.
- Exception handling that needs to roll back partial work or implement compensating actions.
- Anything performance-critical or governor-limit-sensitive.

**Co-existence patterns:**
- Use **Apex Invocable** methods (`@InvocableMethod`) to expose Apex to Flow. Admin orchestrates; developers handle the heavy logic.
- One automation per object: pick Flow OR a trigger, not both, to control execution order.
- Document the choice in a `README.md` next to the automation so future maintainers understand the boundary.

**Common mistake:** treating "Flow first" as "Flow always." A flow with three nested loops, an HTTP callout via External Services, and an Update Records action is doing the work Apex was designed for — refactor before it becomes the next production incident.
