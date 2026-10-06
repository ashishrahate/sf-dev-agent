---
id: pt-platform-event-decoupling
title: Platform Events for cross-system decoupling
category: pattern
severity: info
tags: [platform_event, integration, eventing, async, decoupling]
references:
  - https://developer.salesforce.com/docs/atlas.en-us.platform_events.meta/platform_events/platform_events_intro.htm
---

Platform Events are Salesforce's pub/sub primitive — typed, persisted, replayable messages flowing across triggers, Flow, external systems via CometD/Pub/Sub API, and other Salesforce orgs. Use them to decouple producers from consumers when:

- Multiple subsystems should react to the same change (Account update fires both billing sync AND CRM enrichment).
- A consumer shouldn't slow down the producer's transaction.
- The reaction needs to outlive the producer transaction (publish in trigger, consume async).
- An external system needs the change without a polling loop.

**Define the event:**
Setup → Platform Events → New, e.g. `Account_Updated__e` with fields `Account_Id__c`, `Reason__c`.

**Publish from Apex:**
```apex
List<Account_Updated__e> events = new List<Account_Updated__e>();
for (Account a : Trigger.new) {
    events.add(new Account_Updated__e(
        Account_Id__c = a.Id,
        Reason__c = 'TerritoryChange'
    ));
}
EventBus.publish(events);
```

**Consume in Apex (trigger on event):**
```apex
trigger AccountUpdatedHandler on Account_Updated__e (after insert) {
    for (Account_Updated__e evt : Trigger.new) {
        // own transaction; can callout, DML, etc.
    }
}
```

**Two delivery semantics:**
- **Publish Immediately** (default) — event fires as soon as `EventBus.publish` is called, regardless of the rest of the transaction. Use for fire-and-forget telemetry / audit.
- **Publish After Commit** — event fires only if the transaction commits. Use when consumers should only see events for *successful* writes (the safer default for business logic).

**Replay & retention:** Salesforce retains platform events for 72 hours. External consumers using the Pub/Sub API can resume from a specific replay ID — important for crash recovery.

**Anti-pattern:** using Platform Events as a synchronous-ish RPC. They're async by design; don't try to wait for "the response."
