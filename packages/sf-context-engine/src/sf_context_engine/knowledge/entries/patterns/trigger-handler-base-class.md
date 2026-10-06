---
id: pt-trigger-handler-base
title: TriggerHandler base class pattern
category: pattern
severity: info
tags: [trigger, framework, oop, base_class, recursion]
references:
  - https://github.com/kevinohara80/sfdc-trigger-framework
  - https://github.com/apex-enterprise-patterns/fflib-apex-common
---

A canonical `TriggerHandler` base class encapsulates trigger-context dispatch, recursion control, and bypass switches in one place. Every per-object handler extends it and only implements the specific event hooks it needs.

**Skeleton:**
```apex
public virtual class TriggerHandler {
    private static Set<String> bypassed = new Set<String>();

    public void run() {
        if (bypassed.contains(this.handlerName())) return;
        if (!Trigger.isExecuting) return;

        if (Trigger.isBefore && Trigger.isInsert)  beforeInsert();
        if (Trigger.isBefore && Trigger.isUpdate)  beforeUpdate();
        if (Trigger.isBefore && Trigger.isDelete)  beforeDelete();
        if (Trigger.isAfter  && Trigger.isInsert)  afterInsert();
        if (Trigger.isAfter  && Trigger.isUpdate)  afterUpdate();
        if (Trigger.isAfter  && Trigger.isDelete)  afterDelete();
        if (Trigger.isAfter  && Trigger.isUndelete) afterUndelete();
    }

    protected virtual String handlerName() { return String.valueOf(this).split(':')[0]; }

    public virtual void beforeInsert()   {}
    public virtual void beforeUpdate()   {}
    public virtual void beforeDelete()   {}
    public virtual void afterInsert()    {}
    public virtual void afterUpdate()    {}
    public virtual void afterDelete()    {}
    public virtual void afterUndelete()  {}

    public static void bypass(String name) { bypassed.add(name); }
    public static void clearBypass(String name) { bypassed.remove(name); }
}
```

**Per-object handler:**
```apex
public with sharing class AccountTriggerHandler extends TriggerHandler {
    public override void beforeInsert() { /* ... */ }
    public override void afterUpdate()  { /* ... */ }
}

trigger AccountTrigger on Account (
    before insert, before update, before delete,
    after insert, after update, after delete, after undelete
) {
    new AccountTriggerHandler().run();
}
```

**Production extensions:**
- Recursion control via static guard sets (per handler, per record).
- Bypass switches sourced from `Trigger_Bypass__mdt` instead of static method calls.
- Logging of run/bypass decisions.
- Hooks for ordering when multiple handlers must run on the same object.

Reference implementations: Kevin O'Hara's `sfdc-trigger-framework` (minimal, idiomatic) and `fflib-apex-common` (Apex Enterprise Patterns — domain layer + service layer + selector layer).
