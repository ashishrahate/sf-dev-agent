---
id: gl-batch-chunk-200
title: Batch Apex chunk size (default 200, max 2000)
category: governor_limit
severity: medium
tags: [batch, async, governor_limit, performance]
references:
  - https://developer.salesforce.com/docs/atlas.en-us.apexref.meta/apexref/apex_classes_batch_apex.htm
---

Batch Apex executes the `execute()` method **once per chunk** of records returned by the QueryLocator. Default chunk size is **200**, configurable up to **2,000** via the optional second arg to `Database.executeBatch(myBatch, scopeSize)`.

Each chunk gets its own governor budget — fresh 100 SOQL, 150 DML, 6 MB heap (in async, 12 MB), 60s CPU. So splitting work into smaller chunks gives you more total budget but costs job-overhead time per chunk.

**When to lower chunk size (50–100):**
- The execute() method does heavy per-record work (callouts, complex formulas, queries on related objects).
- You're hitting CPU or heap limits with the default 200.
- You want finer-grained restart on failure.

**When to raise chunk size (500–2000):**
- Lightweight per-record logic (simple field updates).
- Most of the cost is in the SOQL itself, not the per-record loop.
- You want fewer total chunks for faster end-to-end completion.

**Beware:** chunks above 200 sometimes cause `Maximum stack depth has been reached` or longer recovery on partial failure. Profile before raising.

The QueryLocator can return **up to 50 million rows** (vs 50,000 for non-batch SOQL) — that's the main reason to use Batch Apex.
