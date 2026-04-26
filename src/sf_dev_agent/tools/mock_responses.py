"""Canned Salesforce org responses for --mock-org mode.

These look like real sf CLI --json output so the LLM can reason about them
meaningfully without a live org connection.

Scenario: a clean scratch org with a custom Email__c field on Account,
no existing triggers, and a sample account record.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# sf_metadata_describe
# ---------------------------------------------------------------------------

DESCRIBE_APEX_TRIGGER: dict[str, Any] = {
    "status": 0,
    "result": {
        "metadataObjects": [],
        "totalSize": 0,
        "done": True,
        "note": "No ApexTrigger components found in this org.",
    },
}

DESCRIBE_ACCOUNT: dict[str, Any] = {
    "status": 0,
    "result": {
        "fullName": "Account",
        "label": "Account",
        "fields": [
            {"fullName": "Name",        "type": "Text",     "label": "Account Name", "required": True},
            {"fullName": "Email__c",    "type": "Email",    "label": "Email",        "required": False},
            {"fullName": "Phone",       "type": "Phone",    "label": "Phone",        "required": False},
            {"fullName": "Website",     "type": "Url",      "label": "Website",      "required": False},
            {"fullName": "Industry",    "type": "Picklist", "label": "Industry",     "required": False},
            {"fullName": "OwnerId",     "type": "Lookup",   "label": "Owner ID",     "required": True},
        ],
        "sharingModel": "ReadWrite",
        "validationRules": [],
        "triggers": [],
    },
}

DESCRIBE_APEX_CLASS: dict[str, Any] = {
    "status": 0,
    "result": {
        "metadataObjects": [
            {
                "fullName": "SampleUtilityClass",
                "type": "ApexClass",
                "lastModifiedDate": "2025-01-10T12:00:00.000Z",
            }
        ],
        "totalSize": 1,
    },
}

DESCRIBE_FLOW: dict[str, Any] = {
    "status": 0,
    "result": {
        "metadataObjects": [],
        "totalSize": 0,
        "note": "No Flows found on Account object.",
    },
}

DESCRIBE_FALLBACK: dict[str, Any] = {
    "status": 0,
    "result": {
        "metadataObjects": [],
        "totalSize": 0,
    },
}

# ---------------------------------------------------------------------------
# sf_soql_query
# ---------------------------------------------------------------------------

SOQL_ACCOUNT_SAMPLE: dict[str, Any] = {
    "status": 0,
    "result": {
        "totalSize": 2,
        "done": True,
        "records": [
            {
                "attributes": {"type": "Account"},
                "Id": "001MOCK000000001AAA",
                "Name": "Acme Corp",
                "Email__c": "acme@example.com",
            },
            {
                "attributes": {"type": "Account"},
                "Id": "001MOCK000000002AAA",
                "Name": "Globex Inc",
                "Email__c": "globex@example.com",
            },
        ],
    },
}

# ---------------------------------------------------------------------------
# sf_retrieve
# ---------------------------------------------------------------------------

RETRIEVE_EMPTY: dict[str, Any] = {
    "status": 0,
    "result": {
        "files": [],
        "note": "No matching components found in org. This is a fresh scratch org.",
    },
}

# ---------------------------------------------------------------------------
# sf_source_deploy
# ---------------------------------------------------------------------------

DEPLOY_SUCCESS: dict[str, Any] = {
    "status": 0,
    "result": {
        "success": True,
        "id": "0AfMOCK0000000001",
        "status": "Succeeded",
        "numberComponentsTotal": 3,
        "numberComponentsDeployed": 3,
        "numberComponentErrors": 0,
        "numberTestsTotal": 8,
        "numberTestsCompleted": 8,
        "numberTestErrors": 0,
        "details": {
            "componentSuccesses": [
                {"fullName": "AccountTrigger",        "type": "ApexTrigger"},
                {"fullName": "AccountTriggerHandler", "type": "ApexClass"},
                {"fullName": "AccountTriggerTest",    "type": "ApexClass"},
            ],
            "runTestResult": {
                "numFailures": 0,
                "numTestsRun": 8,
                "codeCoverage": [
                    {
                        "name": "AccountTriggerHandler",
                        "numLocations": 24,
                        "numLocationsNotCovered": 2,
                        "coveredPercent": 91.67,
                    }
                ],
            },
        },
    },
}

# ---------------------------------------------------------------------------
# sf_test_run
# ---------------------------------------------------------------------------

TEST_RUN_SUCCESS: dict[str, Any] = {
    "status": 0,
    "result": {
        "summary": {
            "outcome": "Passed",
            "testsRan": 8,
            "passing": 8,
            "failing": 0,
            "skipped": 0,
            "passRate": "100%",
            "failRate": "0%",
            "testExecutionTimeInMs": 1823,
            "codeCoverage": [
                {
                    "name": "AccountTriggerHandler",
                    "coveredPercent": 91.67,
                    "numLinesCovered": 22,
                    "numLinesUncovered": 2,
                }
            ],
        },
        "tests": [
            {"methodName": "testInsertNoDuplicate",   "outcome": "Pass", "message": None},
            {"methodName": "testInsertDuplicate",     "outcome": "Pass", "message": None},
            {"methodName": "testBulkInsert200",       "outcome": "Pass", "message": None},
            {"methodName": "testUpdateEmailChange",   "outcome": "Pass", "message": None},
            {"methodName": "testBlankEmail",          "outcome": "Pass", "message": None},
            {"methodName": "testNullEmail",           "outcome": "Pass", "message": None},
            {"methodName": "testUpdateNoDuplicate",   "outcome": "Pass", "message": None},
            {"methodName": "testUpdateDuplicate",     "outcome": "Pass", "message": None},
        ],
    },
}


# ---------------------------------------------------------------------------
# Router — pick the right canned response based on tool + input
# ---------------------------------------------------------------------------

def get_mock_response(tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    """Return an appropriate canned response for the given tool call."""

    if tool_name == "sf_metadata_describe":
        ctype = tool_input.get("component_type", "").lower()
        if "trigger" in ctype:
            return DESCRIBE_APEX_TRIGGER
        if "object" in ctype:
            return DESCRIBE_ACCOUNT
        if "class" in ctype:
            return DESCRIBE_APEX_CLASS
        if "flow" in ctype:
            return DESCRIBE_FLOW
        return DESCRIBE_FALLBACK

    if tool_name == "sf_soql_query":
        return SOQL_ACCOUNT_SAMPLE

    if tool_name == "sf_retrieve":
        return RETRIEVE_EMPTY

    if tool_name == "sf_source_deploy":
        return DEPLOY_SUCCESS

    if tool_name == "sf_test_run":
        return TEST_RUN_SUCCESS

    if tool_name == "build_metadata_index":
        return {
            "success": True,
            "components_indexed": 0,
            "relationships_indexed": 0,
            "relationships_skipped": 0,
            "parser_errors": [],
            "retrieve_error": None,
            "mocked": True,
            "note": "build_metadata_index skipped in mock-org mode",
        }

    if tool_name == "embed_metadata_index":
        return {
            "embedder": "mock:offline",
            "embedded": 0,
            "skipped_unchanged": 0,
            "skipped_no_source": 0,
            "errors": [],
            "coverage": {},
            "mocked": True,
            "note": "embed_metadata_index skipped in mock-org mode",
        }

    if tool_name == "semantic_search":
        return {
            "query": tool_input.get("query", ""),
            "embedder": "mock:offline",
            "match_count": 0,
            "results": [],
            "mocked": True,
            "note": "semantic_search skipped in mock-org mode",
        }

    if tool_name == "embed_knowledge_base":
        return {
            "embedder": "mock:offline",
            "entries_loaded": 0,
            "entries_updated": 0,
            "entries_skipped_unchanged": 0,
            "embedded": 0,
            "skipped_unchanged": 0,
            "errors": [],
            "coverage": {},
            "mocked": True,
            "note": "embed_knowledge_base skipped in mock-org mode",
        }

    if tool_name == "knowledge_search":
        return {
            "query": tool_input.get("query", ""),
            "embedder": "mock:offline",
            "match_count": 0,
            "results": [],
            "mocked": True,
            "note": "knowledge_search skipped in mock-org mode",
        }

    if tool_name == "retrieve_context":
        return {
            "query": tool_input.get("query", ""),
            "embedder": "mock:offline",
            "hits": [],
            "layer_counts": {
                "semantic": 0, "literal": 0, "knowledge": 0, "graph": 0,
            },
            "estimated_tokens": 0,
            "truncated": 0,
            "layer_errors": [],
            "mocked": True,
            "note": "retrieve_context skipped in mock-org mode",
        }

    # Unknown SF tool — return a generic ok
    return {"status": 0, "result": {"mocked": True, "tool": tool_name}}
