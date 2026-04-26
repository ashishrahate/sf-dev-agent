---
id: bp-custom-metadata-types
title: Custom Metadata Types for configuration
category: best_practice
severity: high
tags: [config, custom_metadata, deployment, architecture]
references:
  - https://developer.salesforce.com/docs/atlas.en-us.apexcode.meta/apexcode/apex_metadata.htm
---

Custom Metadata Types (CMT) are deployable, queryable, type-safe configuration records. They replace nearly every legitimate use case for hardcoded IDs, hardcoded strings, custom settings, and ad-hoc admin records.

**Why CMT > the alternatives:**
- **vs hardcoded values** — admins can change behavior without a deploy.
- **vs Custom Settings (Hierarchy/List)** — CMT records are deployable as part of metadata; Custom Settings have to be migrated as data, which is fragile.
- **vs records of a regular object** — CMT counts don't hit DML governor limits in tests, and they survive sandbox refreshes that would wipe regular records.
- **vs string lookups** — typed `Custom_Field__c.Custom_Value__c` access via `getInstance(DeveloperName)`.

**Patterns CMT solves cleanly:**
- Trigger handler bypass switches: `Trigger_Bypass__mdt` with `IsDisabled__c` per handler name.
- Integration endpoints: `Integration_Endpoint__mdt` with `URL__c`, `API_Version__c`, `Timeout_Ms__c`.
- Feature flags: `Feature_Flag__mdt` with `Is_Enabled__c` and rollout-environment columns.
- Routing rules: `Lead_Routing_Rule__mdt` with priority + conditions.
- Special-record references (replacing hardcoded IDs): `Special_Account_Lookup__mdt` with `Account_Id__c`.

**Reading CMT in Apex:**
```apex
Trigger_Bypass__mdt cfg = Trigger_Bypass__mdt.getInstance('AccountHandler');
if (cfg != null && cfg.Is_Disabled__c) return;
```

CMT does support DML in Apex now (via `Metadata.Operations.enqueueDeployment`), but the typical flow is admin-driven changes through the Setup UI.
