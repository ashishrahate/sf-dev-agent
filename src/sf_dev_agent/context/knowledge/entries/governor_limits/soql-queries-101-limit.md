---
id: gl-soql-queries-101
title: SOQL queries per transaction (101 limit)
category: governor_limit
severity: critical
tags: [soql, governor_limit, bulkification, transaction]
references:
  - https://developer.salesforce.com/docs/atlas.en-us.apexref.meta/apexref/apex_gov_limits.htm
---

Apex transactions are capped at **100 SOQL queries** in synchronous context (200 in async batch/Queueable). Hitting query #101 raises `System.LimitException: Too many SOQL queries: 101` and rolls back the entire transaction.

This limit is the single most common cause of production trigger failures. It exists per *transaction*, not per Apex method, so deeply-nested calls share one budget — a trigger that calls a service that calls a utility that runs a SOQL inside a loop will cap out fast.

**Fix pattern — collect-then-query:**

```apex
// BAD — N queries for N records
for (Account a : accs) {
    Contact c = [SELECT Id FROM Contact WHERE AccountId = :a.Id LIMIT 1];
}

// GOOD — one query for all records
Map<Id, Contact> byAcct = new Map<Id, Contact>();
for (Contact c : [SELECT Id, AccountId FROM Contact WHERE AccountId IN :accs]) {
    byAcct.put(c.AccountId, c);
}
```

Always SELECT in bulk *outside* the loop, then iterate the result. Use `Limits.getQueries()` and `Limits.getLimitQueries()` to inspect runtime budget when debugging.
