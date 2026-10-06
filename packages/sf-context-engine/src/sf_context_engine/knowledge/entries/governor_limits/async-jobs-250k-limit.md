---
id: gl-async-jobs-250k
title: Async job invocations per 24h (250,000 or 200×licenses)
category: governor_limit
severity: high
tags: [async, batch, queueable, future, governor_limit, daily_limit]
references:
  - https://developer.salesforce.com/docs/atlas.en-us.apexref.meta/apexref/apex_gov_limits.htm
---

Asynchronous Apex (Batch + Queueable + Future + Scheduled) shares a daily ceiling of **250,000 invocations OR 200 × number of user licenses**, whichever is greater, per rolling 24-hour window. Crossing it raises `System.AsyncException: Limit on number of async jobs exceeded` and the job *doesn't enqueue at all*.

This limit catches teams off-guard when:
- A trigger enqueues a Queueable per record on a high-volume insert (1M records → 1M jobs → instantly capped).
- A Scheduled Apex job + a Batch chain accidentally interleave and double-fire.
- Future methods are used as a casual "do this later" instead of being deliberately rate-limited.

**Patterns:**
- Bulkify async dispatch: collect all the IDs that need processing and enqueue *one* Queueable that handles the list, not one per record.
- Track invocation count in your own custom-metadata-controlled circuit breaker for high-frequency code paths.
- Prefer Batch Apex (one job, many chunks) over per-record Queueable bursts.
- Monitor `AsyncApexJob.NumberOfErrors` and `Status` via SOQL or Setup → Apex Jobs.

The limit resets on a rolling basis, not at midnight, so a spike at 11:59 PM can block work hours into the next day.
