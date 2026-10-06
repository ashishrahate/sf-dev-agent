"""Lightweight Apex class-reference extractor.

Walks Apex source and returns the set of user-defined class names referenced
via static method calls (`Foo.bar()`), static field/property reads (`Foo.BAR`),
type references (`new Foo()`, `Foo x = ...`), and explicit constructions.

This is a regex-based heuristic — not a real Apex parser. False positives are
mitigated by filtering Apex/Salesforce built-ins and standard sObject names,
and the index's foreign-key constraint silently drops edges to anything that
doesn't actually exist as an indexed component. False negatives (e.g.
references inside fully-qualified namespaced names) are acceptable for slice
1; a future enhancement is to swap this for a real Apex AST.
"""

from __future__ import annotations

import re

# Strip comments before scanning so commented-out class names don't leak in.
_LINE_COMMENT = re.compile(r"//[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_STRING_LITERAL = re.compile(r"'(?:\\.|[^'\\])*'")

# A capitalized identifier followed by `.`, `(`, or used as a type. The
# capitalization filter is a strong signal that the name is a class/type
# (Apex convention), not a variable or method.
_CLASS_REF = re.compile(r"\b([A-Z][A-Za-z0-9_]*)\b")

# Apex / Salesforce built-ins, common standard sObjects, and primitive-ish types.
# Names in here will not be emitted as references. The list is conservative —
# better to under-filter and let the index's FK skip drop the dangling edge
# than to over-filter and miss a real user-defined reference.
_BUILTINS: frozenset[str] = frozenset({
    # Top-level Apex namespaces and helpers
    "System", "Database", "Schema", "Test", "Trigger", "UserInfo", "Limits",
    "Datetime", "Date", "Time", "Type", "JSON", "Math", "URL", "EncodingUtil",
    "Crypto", "Messaging", "Approval", "Dml", "Transaction", "Savepoint",
    "Pattern", "Matcher", "Address", "Location", "Geolocation", "Continuation",
    "DescribeFieldResult", "DescribeSObjectResult",
    "Search", "Quant", "Reports", "ConnectApi", "ChatterAnswers",
    # HTTP / REST / SOAP
    "Http", "HttpRequest", "HttpResponse", "RestRequest", "RestResponse",
    "WebServiceCallout", "Dom",
    # Logging / metadata
    "Logger", "DebugLevel", "Auth",
    # Apex primitives & wrappers
    "String", "Integer", "Long", "Decimal", "Double", "Boolean", "Object",
    "Id", "Blob", "Byte",
    # Generic collection types
    "List", "Map", "Set", "Iterator", "Iterable",
    # Common standard sObjects (extend as the index covers more)
    "Account", "Contact", "Opportunity", "Lead", "Case", "User", "Profile",
    "Group", "Role", "RecordType", "Organization", "Task", "Event", "Note",
    "Attachment", "ContentDocument", "ContentVersion", "EmailMessage",
    "Campaign", "CampaignMember", "Asset", "Product2", "Pricebook2",
    "PricebookEntry", "Quote", "Order", "OrderItem", "Contract",
    # Apex annotations sometimes appear capitalized
    "AuraEnabled", "InvocableMethod", "InvocableVariable", "RestResource",
    "HttpGet", "HttpPost", "HttpPut", "HttpPatch", "HttpDelete", "Future",
    "TestSetup", "TestVisible", "IsTest", "ReadOnly", "RemoteAction",
    "NamespaceAccessible", "JsonAccess", "SuppressWarnings",
    # SOQL keywords sometimes regex-match
    "SELECT", "FROM", "WHERE", "AND", "OR", "NOT", "LIMIT", "OFFSET",
    "GROUP", "BY", "ORDER", "ASC", "DESC", "FOR", "UPDATE", "VIEW",
    "ALL", "ROWS", "NULL", "TRUE", "FALSE",
})


def _strip_noise(source: str) -> str:
    source = _BLOCK_COMMENT.sub(" ", source)
    source = _LINE_COMMENT.sub(" ", source)
    source = _STRING_LITERAL.sub("''", source)
    return source


def extract_class_references(
    source: str,
    exclude: set[str] | None = None,
) -> set[str]:
    """Return the set of user-defined class names referenced by `source`.

    `exclude` lets the caller drop self-references (e.g. a class shouldn't
    emit an edge to itself).
    """
    cleaned = _strip_noise(source)
    raw = set(_CLASS_REF.findall(cleaned))
    candidates = {
        name for name in raw
        if name not in _BUILTINS
        # SOQL-style ALL_CAPS tokens are usually keywords, not class names.
        and not name.isupper()
    }
    if exclude:
        candidates -= exclude
    return candidates
