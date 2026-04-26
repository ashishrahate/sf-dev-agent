---
id: ap-hardcoded-ids
title: Hardcoding record / RecordType / Profile IDs
category: anti_pattern
severity: high
tags: [config, deployment, anti_pattern, record_type, profile]
references:
  - https://developer.salesforce.com/docs/atlas.en-us.apexcode.meta/apexcode/apex_record_types.htm
---

Hardcoding 15- or 18-character Salesforce IDs (`'012700000009XYZ'`) in Apex, Flow, or LWC ties the code to a specific org. The same code deployed to a different sandbox or to production *will compile and pass tests* but produce silently wrong results — different orgs have different IDs.

**Things never to hardcode:**
- Record IDs (Accounts, Cases, etc.)
- RecordType IDs / DeveloperName-based lookups
- Profile IDs / Permission Set IDs
- Group IDs / Queue IDs
- User IDs (especially "the integration user")
- Environment-specific URLs

**Fix patterns:**

| Need | Use |
|---|---|
| RecordType ID | `Schema.SObjectType.Account.getRecordTypeInfosByDeveloperName().get('Customer').getRecordTypeId()` |
| Profile/Group/Queue ID | SOQL by Name / DeveloperName |
| "Special" record | Custom Metadata Type with the lookup, queried via SOQL |
| Environment values | Custom Settings (hierarchy) or Custom Labels |
| User-context behavior | `UserInfo.getUserId()` and feature flags |

Custom Metadata Types are deployable, type-safe, queryable in SOQL and Apex, and free of governor-limit DML restrictions in tests. They're the modern answer to "I need to configure this per-org."
