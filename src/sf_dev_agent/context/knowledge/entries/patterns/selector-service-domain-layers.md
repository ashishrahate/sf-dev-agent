---
id: pt-selector-service-domain
title: Selector / Service / Domain layered architecture (fflib)
category: pattern
severity: info
tags: [architecture, fflib, layers, separation_of_concerns, oop]
references:
  - https://github.com/apex-enterprise-patterns/fflib-apex-common
  - https://andyinthecloud.com/category/apex-enterprise-patterns/
---

Apex Enterprise Patterns (the "fflib" framework popularized by Andrew Fawcett) separates code into three layers, each with a clear responsibility:

**Selector layer** — *all* SOQL.
- One class per sObject (`AccountsSelector`, `ContactsSelector`).
- Methods like `selectById(Set<Id>)`, `selectByOwnerWithContacts(Set<Id>)`.
- Composes WHERE clauses, applies `WITH SECURITY_ENFORCED`, returns typed lists.
- Other layers NEVER write SOQL directly — they call selectors.

**Domain layer** — record-specific business rules.
- One class per sObject extending `fflib_SObjectDomain` (`Accounts`, `Contacts`).
- Trigger handlers delegate to the domain (`Accounts.newInstance(records).onBeforeInsert()`).
- Validation, defaulting, and per-record invariants live here.

**Service layer** — orchestration and cross-object workflows.
- Stateless classes (`AccountService.recalculateLifetimeValue(Set<Id>)`).
- Composes selectors and domains; no SOQL directly.
- Public API for callers (Aura controllers, REST endpoints, scheduled jobs).
- Where transactions are bounded — opens a Unit of Work, commits at the end.

**Why this structure:**
- **Testability** — services are mock-friendly because selectors and domain factories are injected.
- **Reuse** — same selector serves trigger, controller, and Batch Apex.
- **Performance** — deduplicated SOQL paths, easier to bulkify in one place.
- **Security** — FLS/CRUD enforcement centralized in selector.

**Tradeoff:** verbose for small orgs. A 5-class POC doesn't need this; a 500-class shared org absolutely does. Adopt layer by layer (selector first) rather than as a big-bang refactor.

Reference: `fflib-apex-common` provides base classes; `fflib-apex-mocks` provides the mocking framework that makes service-layer tests possible without DML.
