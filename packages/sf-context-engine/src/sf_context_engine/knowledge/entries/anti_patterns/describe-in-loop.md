---
id: ap-describe-in-loop
title: Calling Schema.describe inside a loop
category: anti_pattern
severity: medium
tags: [performance, schema, describe, anti_pattern, cpu]
references:
  - https://developer.salesforce.com/docs/atlas.en-us.apexref.meta/apexref/apex_methods_system_schema.htm
---

`Schema.getGlobalDescribe()`, `SObjectType.<Name>.getDescribe()`, and field describes are **expensive** — each call constructs a fresh metadata snapshot and burns CPU. Calling them per-record in a loop is a classic CPU-limit blowup.

**Bad:**
```apex
for (Account a : accounts) {
    Map<String, Schema.SObjectField> fields = Schema.getGlobalDescribe()
        .get('Account').getDescribe().fields.getMap();
    // ... uses `fields` ...
}
```

**Good — describe once, use many times:**
```apex
Map<String, Schema.SObjectField> accountFields = Account.SObjectType.getDescribe().fields.getMap();
for (Account a : accounts) {
    // ... uses accountFields ...
}
```

**Better — cache at class level:**
```apex
private static final Map<String, Schema.SObjectField> ACCOUNT_FIELDS =
    Account.SObjectType.getDescribe().fields.getMap();
```

Static initializers run once per transaction, so this is the cheapest pattern. Just be mindful of stale describes in long-running async jobs across deployments.

**Modern shortcut:** `Account.SObjectType` (without `.getDescribe()`) gives you a typed `SObjectType` token without the full describe cost — use it whenever you only need the type identity, not the field map.
