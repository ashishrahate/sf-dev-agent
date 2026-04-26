---
id: bp-test-data-factory
title: Centralized TestDataFactory for test data
category: best_practice
severity: high
tags: [testing, factory, testdata, dry, isolation]
references:
  - https://developer.salesforce.com/docs/atlas.en-us.apexcode.meta/apexcode/apex_testing_intro.htm
---

Build every test record through a single `TestDataFactory` (or `TestData`) utility class. Per-test inline record creation duplicates field defaults across hundreds of test methods — when a required field is added in production, every test that doesn't go through the factory has to be updated.

**Skeleton:**
```apex
@isTest
public class TestDataFactory {
    public static Account createAccount(String name) {
        return createAccount(name, true);
    }

    public static Account createAccount(String name, Boolean doInsert) {
        Account a = new Account(
            Name = name,
            BillingCountry = 'US',
            Industry = 'Technology'
        );
        if (doInsert) insert a;
        return a;
    }

    public static List<Contact> createContacts(Id accountId, Integer count) {
        List<Contact> contacts = new List<Contact>();
        for (Integer i = 0; i < count; i++) {
            contacts.add(new Contact(
                LastName = 'Test ' + i,
                AccountId = accountId
            ));
        }
        insert contacts;
        return contacts;
    }
}
```

**Pair with `@TestSetup`:**
```apex
@isTest
private class AccountTriggerTest {
    @TestSetup
    static void setup() {
        Account a = TestDataFactory.createAccount('Acme');
        TestDataFactory.createContacts(a.Id, 200);   // bulk path
    }
    // each @isTest method sees the @TestSetup state, runs in its own DML scope
}
```

**Required fields are the dirty secret of legacy SF orgs** — discovering them during a deploy is painful. The factory is your central place to encode "here are the fields you must always set."
