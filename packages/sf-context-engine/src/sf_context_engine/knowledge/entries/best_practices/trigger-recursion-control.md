---
id: bp-trigger-recursion-control
title: Recursion control in trigger handlers
category: best_practice
severity: high
tags: [trigger, recursion, framework, performance]
references:
  - https://developer.salesforce.com/docs/atlas.en-us.apexcode.meta/apexcode/apex_triggers_bestpract.htm
---

Triggers re-fire when their own logic causes a DML on the same object. A `before update` handler that does `update relatedAccount` may cause the Account trigger to fire again, which may cascade further. Without explicit recursion control, this hits the trigger-stack limit fast and produces hard-to-trace bugs.

**Standard pattern — static guard set:**
```apex
public class AccountTriggerHandler extends TriggerHandler {
    private static Set<Id> processedIds = new Set<Id>();

    public override void beforeUpdate() {
        List<Account> toProcess = new List<Account>();
        for (Account a : (List<Account>) Trigger.new) {
            if (!processedIds.contains(a.Id)) {
                processedIds.add(a.Id);
                toProcess.add(a);
            }
        }
        if (toProcess.isEmpty()) return;
        // ... actual work on toProcess only
    }
}
```

**Why static (not instance) state:**
A `static` field lives for the duration of the transaction, so it persists across re-entries. An instance field on the handler resets every time the trigger framework instantiates a new handler.

**Don't over-guard.**
- Guarding *every* handler globally hides legitimate behavior (e.g. a workflow that intentionally writes the same record twice with different fields).
- Guard at the granularity of the operation, not the record. "Don't run this specific recalculation twice on the same Id" is more precise than "Don't ever process this Id again."

**Bypass switches** in CMT (`Trigger_Bypass__mdt`) complement recursion guards — admins can disable a handler entirely during incidents without code changes.
