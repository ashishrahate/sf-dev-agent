---
id: gl-future-calls-50
title: @future calls per transaction (50 limit)
category: governor_limit
severity: medium
tags: [future, async, governor_limit, integration]
references:
  - https://developer.salesforce.com/docs/atlas.en-us.apexref.meta/apexref/apex_gov_limits.htm
---

A single Apex transaction can enqueue at most **50 `@future` method calls**. The 51st call raises `System.AsyncException`. This is in addition to the daily async-job limit (250,000 / 200 × licenses).

`@future` is the oldest async mechanism in Apex and the one with the most rough edges:
- You cannot call a `@future` method from another `@future` method.
- You cannot call `@future` from Batch Apex.
- Future methods can't take sObjects as arguments — only primitives and primitive collections (Lists, Sets, Maps).
- No way to monitor or chain reliably.

**Modern alternative: Queueable.**
Queueable jobs are now the default async choice. They:
- Accept sObjects and complex types as inputs (constructor params).
- Return a job Id you can monitor in `AsyncApexJob`.
- Chain reliably with `System.enqueueJob(...)` from inside the `execute()` method.
- Have a separate (50 chained jobs sync, 1 chain in transactions stack) limit but are far more flexible.

Use `@future(callout=true)` only when you need to make a callout from a trigger context and don't need the chaining/monitoring features. Default to Queueable otherwise.
