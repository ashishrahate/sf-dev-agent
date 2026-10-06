---
id: gl-dml-statements-150
title: DML statements per transaction (150 limit)
category: governor_limit
severity: critical
tags: [dml, governor_limit, bulkification, transaction]
references:
  - https://developer.salesforce.com/docs/atlas.en-us.apexref.meta/apexref/apex_gov_limits.htm
---

Apex transactions are capped at **150 DML statements** (insert / update / upsert / delete / undelete / merge). The 151st call raises `System.LimitException: Too many DML statements: 151` and rolls back the transaction.

Each call to `insert acc` counts as one statement *regardless of how many records are in the list*. So `insert listOfAccounts` (one statement, 200 records) is fine; `for (Account a : accs) { insert a; }` (200 statements, 200 records) is broken.

**Fix pattern — accumulate-then-DML:**

```apex
// BAD — 200 DML statements
for (Account a : toUpdate) {
    a.Name = a.Name.toUpperCase();
    update a;
}

// GOOD — 1 DML statement
List<Account> updates = new List<Account>();
for (Account a : toUpdate) {
    a.Name = a.Name.toUpperCase();
    updates.add(a);
}
update updates;
```

A separate counter tracks **DML rows** (10,000 per transaction); be aware of it, but the statement limit hits first in nearly every realistic case.
