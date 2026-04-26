---
id: bp-test-coverage-assertions
title: Meaningful test coverage and assertions
category: best_practice
severity: high
tags: [testing, coverage, assertions, deployment, quality]
references:
  - https://developer.salesforce.com/docs/atlas.en-us.apexcode.meta/apexcode/apex_testing_code_coverage.htm
---

Salesforce requires **75% Apex code coverage** to deploy to production, but coverage alone is a meaningless number. A test that runs 100% of a class without any `System.assert` calls passes coverage and asserts nothing about behavior — the next refactor silently breaks production.

**Three rules for tests that are actually useful:**

**1. Assert behavior, not just absence of exception.**
```apex
// useless — passes if the method returns ANYTHING
Account result = AccountService.findCustomer('X');

// useful
System.assertEquals('Acme', result.Name);
System.assertEquals(true, result.IsActive__c);
System.assertNotEquals(null, result.Id);
```

**2. Cover the bulk path AND the boundary cases.**
- 1 record (trivial path)
- 200 records (bulk path)
- 0 records (defensive — the trigger should be a no-op)
- Records that hit a validation rule
- Records owned by a different user (sharing)

**3. Test failure paths, not just happy paths.**
- `try { ... } catch (Exception e) { System.assertEquals('expected msg', e.getMessage()); }` for errors that should fire.
- Verify rollback: if your code throws after partial DML, assert that the DB state was not partially mutated.

**Modern assertions** — `System.Assert.areEqual(...)`, `Assert.isTrue(...)`, `Assert.fail(...)` (API v53+) give better failure messages than `System.assertEquals`. Prefer them in new code.

Aim for **85%+ coverage** of the changed code in a PR, with one assert per behavior. Don't game coverage by writing tests that just call the method.
