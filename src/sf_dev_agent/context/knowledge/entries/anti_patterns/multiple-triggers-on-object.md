---
id: ap-multiple-triggers
title: Multiple Apex triggers on the same object
category: anti_pattern
severity: high
tags: [trigger, ordering, anti_pattern, framework]
references:
  - https://developer.salesforce.com/docs/atlas.en-us.apexcode.meta/apexcode/apex_triggers_bestpract.htm
---

Salesforce does **not guarantee execution order** when multiple triggers exist on the same sObject. Two triggers on Account both `before insert` may fire in any order, and that order can change with metadata API deployments, packaged installs, or platform updates. Bugs that surface this way are nearly impossible to reproduce in dev sandboxes.

**The rule: one trigger per object.** All logic for that object goes through that single trigger, which delegates to a handler class that controls execution order explicitly.

**Pattern — Trigger Handler framework:**

```apex
trigger AccountTrigger on Account (
    before insert, before update, before delete,
    after insert,  after update,  after delete, after undelete
) {
    new AccountTriggerHandler().run();
}

public with sharing class AccountTriggerHandler extends TriggerHandler {
    public override void beforeInsert() { /* validate, default fields */ }
    public override void afterUpdate()  { /* publish events, sync */ }
    // ...
}
```

The handler base class typically provides:
- Recursion guards (don't re-fire on the same record set in the same transaction).
- Bypass switches keyed off custom metadata (so admins can disable handlers in incidents).
- Ordering hooks (call sub-handlers in a defined sequence).

**If you inherit a multi-trigger codebase:** the cleanup pattern is to introduce a single trigger with a handler, then progressively port and delete the legacy triggers. Run integration tests at each step.
