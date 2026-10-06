---
id: pt-queueable-chaining
title: Queueable chaining for long-running async work
category: pattern
severity: info
tags: [async, queueable, chaining, long_running, integration]
references:
  - https://developer.salesforce.com/docs/atlas.en-us.apexcode.meta/apexcode/apex_queueing_jobs.htm
---

When work is too big for a single transaction's CPU/SOQL/heap budget but doesn't fit Batch Apex's record-stream model (e.g. paginated remote API consumption), chain Queueable jobs. Each Queueable runs in its own transaction with a fresh budget; chaining is just `System.enqueueJob(nextJob)` from inside the current job's `execute()`.

**Skeleton:**
```apex
public class IntegrationSyncQueueable implements Queueable, Database.AllowsCallouts {
    private Integer page;
    private List<Id> processed;

    public IntegrationSyncQueueable(Integer page, List<Id> processed) {
        this.page = page;
        this.processed = processed;
    }

    public void execute(QueueableContext ctx) {
        // 1. Make remote callout for this page
        HttpResponse res = fetchPage(page);
        // 2. Process and DML
        List<Account> updates = parse(res);
        update updates;
        for (Account a : updates) processed.add(a.Id);

        // 3. Continue if more pages
        if (hasNextPage(res)) {
            System.enqueueJob(new IntegrationSyncQueueable(page + 1, processed));
        } else {
            // Final cleanup, audit log, etc.
        }
    }
}
```

**Rules and gotchas:**
- Synchronous chain depth is limited (50 in regular orgs, 5 in scratch). Each call to `enqueueJob` from within a Queueable counts as one chain step.
- For unbounded chaining (e.g. paginated APIs of unknown length), build in a max-iteration counter and circuit-breaker.
- Pass STATE via the constructor — don't rely on global variables. Apex doesn't share mutable state across transactions.
- `Database.AllowsCallouts` lets the Queueable make HTTP calls; required if you're using this for integration sync.

**vs Batch Apex:** use Batch when work can be expressed as "iterate this SOQL result, do X per chunk." Use Queueable chains when work is "do step A, then step B that depends on A, then step C..." that's not list-shaped.

**vs `@future`:** Queueable is the modern replacement. Future has more limits, no chaining, and no monitoring.
