---
id: ap-see-all-data-true
title: Using @isTest(SeeAllData=true) in test classes
category: anti_pattern
severity: high
tags: [testing, isolation, anti_pattern, sandbox]
references:
  - https://developer.salesforce.com/docs/atlas.en-us.apexcode.meta/apexcode/apex_testing_isolation_data.htm
---

Setting `@isTest(SeeAllData=true)` makes the test see all org data (records, custom settings, custom metadata visibility, etc.) instead of being isolated. This breaks the test-isolation contract that's existed since API v24.

**Why this is bad:**
- Tests pass in dev and fail in CI / different sandboxes.
- Tests pass when an admin happens to leave a record in the org and break when they delete it.
- Code coverage measurements are misleading — you're testing against ambient state, not behavior.
- Slow CI: every test loads the full org's record visibility.

**The legitimate exceptions** (fewer than people think):
- Apex code that absolutely depends on Custom Settings or Custom Metadata that is *not* test-creatable. (Both *are* now test-creatable in modern Apex; this exception is mostly historical.)
- Tests for code that uses `Schema.getGlobalDescribe()` against standard objects in unusual ways.

**Fix pattern — `@TestSetup` + record factories:**

```apex
@isTest
private class AccountHandlerTest {
    @TestSetup
    static void setup() {
        Account a = new Account(Name='Test', BillingCountry='US');
        insert a;
    }
    @isTest static void testWidgetCalculation() { /* ... */ }
}
```

Centralize record creation in a `TestDataFactory` class that every test uses. New tests should never need `SeeAllData=true`.
