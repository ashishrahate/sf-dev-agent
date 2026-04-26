---
id: ap-soql-in-loop
title: SOQL inside a loop
category: anti_pattern
severity: critical
tags: [soql, bulkification, governor_limit, anti_pattern]
references:
  - https://developer.salesforce.com/docs/atlas.en-us.apexcode.meta/apexcode/apex_bulk_design_patterns.htm
---

Running a `SELECT` inside a `for` loop is the single most common cause of production trigger failures. Each iteration consumes one of the transaction's 100 SOQL queries; processing 200 records hits limit #101 long before the loop completes.

**Bad:**

```apex
for (Account a : Trigger.new) {
    List<Contact> related = [SELECT Id FROM Contact WHERE AccountId = :a.Id];
    // ...
}
```

**Good — collect IDs, query once, group results:**

```apex
Set<Id> acctIds = new Map<Id, Account>(Trigger.new).keySet();

Map<Id, List<Contact>> contactsByAcct = new Map<Id, List<Contact>>();
for (Contact c : [SELECT Id, AccountId FROM Contact WHERE AccountId IN :acctIds]) {
    if (!contactsByAcct.containsKey(c.AccountId)) {
        contactsByAcct.put(c.AccountId, new List<Contact>());
    }
    contactsByAcct.get(c.AccountId).add(c);
}

for (Account a : Trigger.new) {
    List<Contact> related = contactsByAcct.get(a.Id);
    // ...
}
```

The same rule applies to **DML in a loop** — collect into a `List<sObject>` and run one DML statement after the loop.

PMD's `OperationWithLimitsInLoop` rule catches this. Run lint before deploy.
