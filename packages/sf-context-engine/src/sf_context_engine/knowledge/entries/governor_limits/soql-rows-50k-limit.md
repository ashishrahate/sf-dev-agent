---
id: gl-soql-rows-50k
title: SOQL query rows per transaction (50,000 limit)
category: governor_limit
severity: high
tags: [soql, governor_limit, query, batch]
references:
  - https://developer.salesforce.com/docs/atlas.en-us.apexref.meta/apexref/apex_gov_limits.htm
---

A single Apex transaction can retrieve at most **50,000 rows** across all SOQL queries combined. Exceeding it raises `System.LimitException: Too many query rows: 50001`.

Counting rules:
- Aggregate queries (`SELECT COUNT()`) count their *result rows*, not the rows they aggregate over.
- `Database.QueryLocator` in Batch Apex bypasses this limit — it can iterate 50 million rows across batches.
- Async (Batch / Queueable / Future) gets the same per-transaction 50,000 ceiling, but Batch's QueryLocator is the documented escape hatch for very large result sets.

**Fix patterns:**

- Use `WHERE` clauses tight enough that you never need >50k rows in one transaction.
- Aggregate in SOQL, not in Apex: `SELECT COUNT() FROM Lead WHERE Status = 'New'` instead of pulling all leads and counting in code.
- Move large-volume work to Batch Apex where `Database.QueryLocator` streams.
- Use `LIMIT` defensively on exploratory queries, even when you "know" the result is small.
