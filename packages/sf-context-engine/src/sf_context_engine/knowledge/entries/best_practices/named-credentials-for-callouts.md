---
id: bp-named-credentials
title: Named Credentials for HTTP callouts
category: best_practice
severity: high
tags: [callout, security, named_credential, integration, oauth]
references:
  - https://developer.salesforce.com/docs/atlas.en-us.apexcode.meta/apexcode/apex_callouts_named_credentials.htm
---

Use **Named Credentials** for every HTTP callout. They centralize the endpoint URL, authentication method (Basic, OAuth 2.0, JWT, AWS Signed), and certificate config — so credentials never live in Apex source, in Custom Settings, or in Custom Metadata as plaintext.

**Why this matters:**
- Apex source containing a hardcoded API key is a one-PR-screenshot away from being public.
- Custom Settings of type "API Key" appear in `sf data export` and Setup Audit Trail in plaintext.
- Named Credentials encrypt at rest, redact in describe calls, and rotate independently of code deployments.

**Pattern:**
```apex
HttpRequest req = new HttpRequest();
req.setEndpoint('callout:My_Named_Credential/v1/customers/' + customerId);
req.setMethod('GET');
HttpResponse res = new Http().send(req);
```

The `callout:My_Named_Credential` prefix tells Apex to:
1. Resolve the named credential's `URL`.
2. Apply its `Authorization` header (OAuth token refresh handled automatically).
3. Apply its outbound certificate (mTLS) if configured.

**External Credentials** (the modern decomposition introduced 2023+) split out auth from URL — an "Auth Provider" + "External Credential" + "Named Credential" triple supports rotating clients sharing one auth target. Use this layering for any new integration.

**Never hand-roll OAuth in Apex.** Token refresh, expiry, and PKCE all live in the platform. Hand-rolled OAuth is a security review failure.
