---
id: bp-bulkified-trigger-handler
title: Bulkified trigger handlers
category: best_practice
severity: critical
tags: [trigger, bulkification, performance, governor_limit]
references:
  - https://developer.salesforce.com/docs/atlas.en-us.apexcode.meta/apexcode/apex_triggers_bulk.htm
---

Every trigger handler **must** be written to handle a list of records (1 to 200), not one record at a time. The Salesforce platform invokes triggers with whatever number of records the saving DML had — Data Loader, API integrations, and Flow can all send batches up to 200 in one invocation.

**Bulkification rules:**
1. Iterate `Trigger.new` / `Trigger.old` once.
2. Collect related IDs into a `Set<Id>` during the iteration.
3. SOQL the related records once with `WHERE Id IN :ids`.
4. Group results into a `Map<Id, ...>` so per-record logic can look them up in O(1).
5. DML once after the per-record loop.

**Skeleton:**
```apex
public override void beforeUpdate() {
    Set<Id> ownerIds = new Set<Id>();
    for (Account a : (List<Account>) Trigger.new) {
        ownerIds.add(a.OwnerId);
    }

    Map<Id, User> ownersById = new Map<Id, User>([
        SELECT Id, Profile.Name FROM User WHERE Id IN :ownerIds
    ]);

    for (Account a : (List<Account>) Trigger.new) {
        User owner = ownersById.get(a.OwnerId);
        if (owner.Profile.Name == 'System Administrator') {
            a.Reviewed_By_Admin__c = true;
        }
    }
    // No DML here — Apex auto-saves Trigger.new in before contexts
}
```

**Test the bulk path.** A test that inserts one record exercises only the trivial case. Insert 200 records in at least one test method to exercise governor-limit boundaries.
