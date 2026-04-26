---
id: bp-null-safe-soql-access
title: Null-safe SOQL access and the safe-navigation operator
category: best_practice
severity: medium
tags: [null_safety, soql, apex, defensive_coding]
references:
  - https://developer.salesforce.com/docs/atlas.en-us.apexcode.meta/apexcode/apex_classes_safe_navigation_operator.htm
---

SOQL with `LIMIT 1` returning zero rows + indexing into `[0]` is a classic `ListException: List index out of bounds` in production. The same shape with related-object navigation (`acc.Owner.Profile.Name`) trips `NullPointerException` whenever the related record was deleted or the user lacks access.

**Three patterns to use consistently:**

**1. Safe-navigation operator `?.` (API v53+):**
```apex
String profileName = acc?.Owner?.Profile?.Name;   // null if any link is null
```

**2. Defensive size check before indexing:**
```apex
List<Account> matches = [SELECT Id FROM Account WHERE Name = :n LIMIT 1];
Account acc = matches.isEmpty() ? null : matches[0];
```

**3. `Map<Id, sObject>` over `for ... LIMIT 1`:**
```apex
Map<Id, Account> byId = new Map<Id, Account>([
    SELECT Id, Name FROM Account WHERE Id IN :ids
]);
Account acc = byId.get(someId);   // null if not found, no exception
```

**SOQL relationship navigation gotchas:**
- A child relationship like `acc.Contacts` is a `List<Contact>` that may be empty but is never null.
- A parent relationship like `acc.Owner` IS null if the parent was deleted, even though the FK column has a value.
- `WITH SECURITY_ENFORCED` on a SOQL hides fields the user can't see — accessing them in Apex post-query yields null.

**Test the empty / null path explicitly** — `@isTest` methods that only insert valid records never exercise the failure mode that hits production first.
