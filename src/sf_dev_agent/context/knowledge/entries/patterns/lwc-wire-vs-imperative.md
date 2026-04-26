---
id: pt-lwc-wire-vs-imperative
title: LWC @wire vs imperative Apex calls
category: pattern
severity: info
tags: [lwc, lightning, apex, frontend, reactivity]
references:
  - https://developer.salesforce.com/docs/component-library/documentation/en/lwc/lwc.apex
---

Lightning Web Components have two ways to call Apex: `@wire` (declarative, reactive) and imperative (`callApex(params)`). Pick by intent.

**Use `@wire` when:**
- You want the data to refresh automatically when reactive parameters change.
- You want the framework to manage caching and request de-duplication.
- The call doesn't have side effects (it should be `@AuraEnabled(cacheable=true)`).

```javascript
import { LightningElement, wire } from 'lwc';
import getAccount from '@salesforce/apex/AccountController.getAccount';

export default class AccountCard extends LightningElement {
    @api recordId;
    @wire(getAccount, { id: '$recordId' })
    accountResponse;
    // accountResponse.data and accountResponse.error update automatically
}
```

**Use imperative when:**
- The action has side effects (DML, callout, modifying server state).
- The user must explicitly trigger it (button click, form submit).
- You need fine-grained loading/error UI control.

```javascript
import saveAccount from '@salesforce/apex/AccountController.saveAccount';

async handleSave() {
    try {
        this.isSaving = true;
        await saveAccount({ accountJson: JSON.stringify(this.draft) });
        this.dispatchEvent(new ShowToastEvent({ title: 'Saved' }));
    } catch (e) {
        this.error = e.body.message;
    } finally {
        this.isSaving = false;
    }
}
```

**Cacheable rule:** `@wire` requires the Apex method to be marked `@AuraEnabled(cacheable=true)` — that's the platform's promise that the call has no server-side side effects. The cache is a 5-minute browser cache shared across components calling the same method with the same params.

**Refresh after a mutation:** call `refreshApex(this.accountResponse)` to invalidate the cache and re-pull. Without it, the wire data stays stale until navigation.
