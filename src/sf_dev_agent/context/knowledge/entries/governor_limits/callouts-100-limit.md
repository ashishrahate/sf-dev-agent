---
id: gl-callouts-100
title: HTTP callouts per transaction (100 limit) and 120-second cap
category: governor_limit
severity: high
tags: [callout, http, governor_limit, async, integration]
references:
  - https://developer.salesforce.com/docs/atlas.en-us.apexref.meta/apexref/apex_gov_limits.htm
---

Apex transactions are limited to **100 callouts** with a cumulative **120 seconds** of callout time. Each individual callout is also capped at **120 seconds** (timeout configurable down to 1ms). Crossing either raises a `LimitException` and rolls back the transaction.

**Hard rules around callouts:**
- Callouts cannot occur after a DML statement in the same transaction. Either do all DML first OR all callouts first, OR split with `@future(callout=true)` / Queueable.
- Callouts are not allowed in triggers directly — call into `@future(callout=true)` or enqueue a Queueable.
- Always set an explicit `setTimeout()` shorter than the default; don't let one slow remote pin your transaction.

**Patterns:**
- Batch outbound calls: send 100 IDs in one POST instead of 100 single calls.
- Use `Continuation` for long-running synchronous web requests in Lightning components.
- Wrap callouts in retry logic, but cap retries — burning all 100 on retries to a failing endpoint just delays the inevitable error.
- Log every callout (request, response, latency) to a custom object for incident debugging.
