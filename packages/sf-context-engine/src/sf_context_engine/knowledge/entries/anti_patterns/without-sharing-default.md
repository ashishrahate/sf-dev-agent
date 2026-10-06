---
id: ap-without-sharing-default
title: Using "without sharing" as the default
category: anti_pattern
severity: high
tags: [security, sharing, anti_pattern, fls, crud]
references:
  - https://developer.salesforce.com/docs/atlas.en-us.apexcode.meta/apexcode/apex_classes_keywords_sharing.htm
---

Apex classes default to `inherited sharing` if you don't specify, but if **the entry-point class is not annotated**, it executes in **system context** — bypassing the running user's record-level sharing. This is rarely what you want.

**The keywords:**
- `with sharing` — enforces the running user's sharing rules. Most application code should use this.
- `without sharing` — explicitly bypasses sharing. Use *only* for genuinely privileged operations (data migration, system audit, admin-triggered cleanup).
- `inherited sharing` — adopts the calling context's sharing. Use for utility classes that should match the caller. Default for `@AuraEnabled` controllers.

**Defaults you should follow:**
- Trigger handlers: `with sharing` (run as the user who saved the record).
- Controllers (`@AuraEnabled`, `@RestResource`): `with sharing` unless the use case is explicitly admin-only.
- Service / domain classes: `inherited sharing`.
- Batch jobs: `with sharing` unless the job is meant to act as a privileged actor; document the choice in the class header either way.

**Sharing only enforces row-level visibility.** It does NOT enforce field-level security or CRUD. You still need `Security.stripInaccessible(...)`, `Schema.DescribeFieldResult.isAccessible/Updateable`, or `WITH SECURITY_ENFORCED` in SOQL to enforce those — see the related anti-pattern entry on FLS bypass.
