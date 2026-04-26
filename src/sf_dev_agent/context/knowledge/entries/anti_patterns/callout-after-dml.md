---
id: ap-callout-after-dml
title: HTTP callout after DML in the same transaction
category: anti_pattern
severity: high
tags: [callout, dml, anti_pattern, transaction, integration]
references:
  - https://developer.salesforce.com/docs/atlas.en-us.apexcode.meta/apexcode/apex_callouts_dml_limitation.htm
---

Apex transactions cannot perform an HTTP callout *after* any DML statement (insert / update / upsert / delete / merge). Attempting it raises `CalloutException: You have uncommitted work pending. Please commit or rollback before calling out`.

This is **not** a limit you can raise — it's a transactional integrity rule. The platform doesn't want a remote system to see data Apex is about to roll back.

**Three legitimate fixes:**

**1. Reorder — all callouts first, all DML last:**
```apex
HttpResponse res = makeCallout();           // before any DML
Account a = parseAndBuildAccount(res);
insert a;
```

**2. Move the callout to async:**
```apex
trigger AccountTrigger on Account (after insert) {
    AccountAsync.notifyExternal(Trigger.newMap.keySet());
}

public class AccountAsync {
    @future(callout=true)
    public static void notifyExternal(Set<Id> accountIds) {
        // safe — fresh transaction, no pending DML
    }
}
```

**3. Queueable with chained DML:**
```apex
public class AccountQueueable implements Queueable, Database.AllowsCallouts {
    public void execute(QueueableContext ctx) {
        HttpResponse res = ...;
        // do DML here in this fresh transaction
    }
}
```

Use `@future(callout=true)` for the simplest fire-and-forget case; use Queueable when you need parameters that are sObjects, or chaining, or a job ID to monitor.
