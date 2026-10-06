---
id: gl-cpu-time
title: Apex CPU time limit (10s sync / 60s async)
category: governor_limit
severity: critical
tags: [cpu, performance, governor_limit, async]
references:
  - https://developer.salesforce.com/docs/atlas.en-us.apexref.meta/apexref/apex_gov_limits.htm
---

Apex transactions are limited to **10,000 ms of CPU time** in synchronous context and **60,000 ms** in async (Batch / Queueable / Future / Scheduled). Exceeding it raises `System.LimitException: Apex CPU time limit exceeded`.

CPU time excludes:
- Time spent waiting for SOQL / DML / callouts (those have separate limits)
- Time in declarative automation (Flow steps, Workflow rules) — but their CPU still counts against the transaction's total when triggered from Apex

**What burns CPU:**
- Nested loops over large collections (O(n²) Map building)
- JSON serialize/parse on big payloads
- Regex matching on long strings
- String concatenation in loops (use `String.join`)
- Recursive trigger handlers without proper static guards

**Diagnosis:** add `System.debug('CPU=' + Limits.getCpuTime())` checkpoints to localize the hot path. Move heavy compute to async (Queueable + chaining) when possible — that buys 6× more budget.

CPU is the limit you most often *don't* notice in dev sandboxes; production data volume is what tips it over.
