---
id: gl-heap-size
title: Heap size limit (6MB sync / 12MB async)
category: governor_limit
severity: high
tags: [heap, memory, governor_limit, batch]
references:
  - https://developer.salesforce.com/docs/atlas.en-us.apexref.meta/apexref/apex_gov_limits.htm
---

Apex enforces a **6 MB heap** in synchronous transactions and **12 MB** in async (Batch, Queueable, Future, Scheduled). Crossing it raises `System.LimitException: Apex heap size too large: <bytes>`.

Common heap pitfalls:

- Loading large query results entirely into a `List<sObject>` instead of streaming with `Database.QueryLocator`.
- Building giant `Map<Id, ...>` keyed off all records of a high-volume object.
- Holding raw `Blob` payloads (PDFs, attachments) longer than necessary.
- Concatenating into a `String` inside a loop — every append allocates a new string; switch to `String.join` or `String.format`.

**Patterns that help:**

- Use `Database.QueryLocator` in Batch Apex — it streams chunks of 200 rows, never materializing the full result.
- Null out large variables (`bigMap = null;`) once you're done with them; the runtime can't always GC eagerly.
- Use `Limits.getHeapSize()` / `Limits.getLimitHeapSize()` to instrument runtime heap during debug.

Heap is per-transaction, not per-method — recursive triggers and re-entrant flows compound it fast.
