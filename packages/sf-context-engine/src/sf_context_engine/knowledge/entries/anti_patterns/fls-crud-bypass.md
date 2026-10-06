---
id: ap-fls-crud-bypass
title: Skipping FLS/CRUD checks before DML
category: anti_pattern
severity: critical
tags: [security, fls, crud, anti_pattern, soql]
references:
  - https://developer.salesforce.com/docs/atlas.en-us.apexcode.meta/apexcode/apex_security_sharing_chapter.htm
  - https://developer.salesforce.com/docs/atlas.en-us.apexref.meta/apexref/apex_class_System_Security.htm
---

`with sharing` enforces row-level visibility — it does **not** enforce field-level security (FLS) or object-level CRUD. Apex code that performs DML without FLS/CRUD checks can leak or write data the running user shouldn't see, even with `with sharing`. This regularly fails security review and is a real-world breach vector.

**Pick one of these enforcement patterns:**

**1. `Security.stripInaccessible` (recommended for collections):**
```apex
SObjectAccessDecision dec = Security.stripInaccessible(
    AccessType.UPDATABLE, accountList, true   // throw on inaccessible fields
);
update dec.getRecords();
```

**2. `WITH SECURITY_ENFORCED` in SOQL:**
```apex
List<Account> accs = [
    SELECT Id, Name, Custom_Email__c
    FROM Account
    WHERE Id IN :ids
    WITH SECURITY_ENFORCED
];
```
Throws `QueryException` if the user can't see any selected field.

**3. Manual describe checks** (verbose; for one-off field/object guards):
```apex
if (!Schema.sObjectType.Account.isUpdateable()) {
    throw new SecurityException('Cannot update Account');
}
if (!Schema.sObjectType.Account.fields.Custom_Email__c.isUpdateable()) { /* ... */ }
```

**Where this matters most:**
- `@AuraEnabled` controllers — these are direct external entry points.
- `@RestResource` web services.
- Anything wrapping `Database.update(records, false)` to "ignore failures" — that path silently writes inaccessible fields too.

PMD's `ApexCRUDViolation` rule flags missing checks. Run lint before deploy.
