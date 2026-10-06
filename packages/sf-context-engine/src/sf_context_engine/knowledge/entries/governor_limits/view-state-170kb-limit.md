---
id: gl-view-state-170kb
title: View state limit (170 KB) for Visualforce pages
category: governor_limit
severity: medium
tags: [visualforce, view_state, governor_limit, ui]
references:
  - https://developer.salesforce.com/docs/atlas.en-us.pages.meta/pages/pages_view_state.htm
---

Visualforce pages cap **view state at 170 KB**. Crossing it raises `Maximum view state size limit (170KB) exceeded` and fails the page render or postback.

View state stores everything between requests on a stateful Visualforce page: controller member variables (including SOQL results assigned to public fields), wizard step data, component bindings, and the back-end map of the visualforce component tree.

**Common causes:**
- Storing large `List<sObject>` results on the controller for use across postbacks.
- Wide queries (`SELECT Id, Name, ... 30+ fields, ... FROM Account LIMIT 1000`) bound to a public list field.
- Maps keyed off Id with large values.
- `transient` keyword forgotten on temporary working variables.

**Patterns:**
- Mark working variables `transient` so they're excluded from view state.
- Page-paginate large result sets via `StandardSetController` (Salesforce-managed, doesn't bloat view state).
- Move the page to LWC if view state is the dominant constraint — Lightning has no view state at all.

This limit is **page-specific** and doesn't apply to LWC, Aura, or REST APIs.
