"""Delta-refresh planning for the metadata index.

The full-refresh path (always retrieve every component of every requested type)
gets expensive past tens of components. Delta refresh narrows each rebuild to
just the components whose `LastModifiedDate` in the org is newer than our last
indexing — plus components newly created since, plus deletions to clean out.

Flow:
    1. fetch_org_inventory(alias, types) -> OrgInventory
       Hits the Tooling API once per type (twice for CustomObject — see below)
       to pull (Id, Name, LastModifiedDate) tuples.
    2. compute_deltas(inventory, index) -> DeltaPlan
       String-compares ISO-8601 timestamps to classify each component as
       to_fetch (new or changed) or to_delete (in index but not in org).
    3. The orchestrator (build_index) consumes the plan: targeted retrieve for
       to_fetch, MetadataIndex.delete_components for to_delete.

CustomObject takes two Tooling queries:
    - `CustomObject`: returns object-level LastModifiedDate. Only catches
      changes to the object's own metadata (label, sharingModel, etc.).
    - `CustomField` GROUPed by parent: returns max(field.LastModifiedDate) per
      object, so adding or editing a single field still triggers a re-fetch
      even when the object's own row hasn't moved.

Per-object timestamp = max(object_meta_modified, max_field_modified). When
the orchestrator re-fetches a CustomObject, it also calls
`MetadataIndex.delete_children_of(...)` to drop stale CustomField rows whose
*.field-meta.xml file no longer exists in the org's source.

Standard objects (Account, Contact, etc.) don't appear in `CustomObject`
queries and are out of scope for this slice — they fall back to full retrieve
when listed alongside an unsupported component type.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Iterable

from sf_dev_agent.context.parsers.base import Parser

logger = logging.getLogger(__name__)


# Component types this slice supports for delta refresh. Anything outside
# this set is excluded from the inventory pass and falls back to full retrieve.
SUPPORTED_DELTA_TYPES: frozenset[str] = frozenset(
    {"ApexClass", "ApexTrigger", "CustomObject"}
)


@dataclass
class OrgComponent:
    """One row from a Tooling API inventory query."""
    component_type: str
    api_name: str
    last_modified_at: str   # ISO-8601, directly comparable to components.last_indexed_at

    @property
    def component_id(self) -> str:
        return Parser.make_id(self.component_type, self.api_name)


@dataclass
class OrgInventory:
    """All components in the org, grouped by type."""
    components: list[OrgComponent] = field(default_factory=list)
    types_queried: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def by_id(self) -> dict[str, OrgComponent]:
        return {c.component_id: c for c in self.components}


@dataclass
class DeltaPlan:
    """Classification of every relevant component into one of three buckets."""
    to_fetch: list[str] = field(default_factory=list)        # canonical component_ids to retrieve
    to_delete: list[str] = field(default_factory=list)       # component_ids to remove from index
    unchanged: list[str] = field(default_factory=list)       # component_ids that match — no work needed
    unsupported_types: list[str] = field(default_factory=list)  # types delta couldn't cover; caller must full-fetch

    @property
    def is_empty(self) -> bool:
        return not (self.to_fetch or self.to_delete)


# ---------------------------------------------------------------------------
# Org inventory via Tooling API
# ---------------------------------------------------------------------------

def _sf_exe() -> str:
    return "sf.cmd" if sys.platform == "win32" else "sf"


# Tooling API SOQL per supported type. Each row must yield (api_name, last_modified).
# CustomObject is special-cased — see _fetch_custom_object_inventory.
_INVENTORY_QUERIES: dict[str, str] = {
    "ApexClass": "SELECT Id, Name, LastModifiedDate FROM ApexClass",
    "ApexTrigger": "SELECT Id, Name, LastModifiedDate FROM ApexTrigger",
}


def _query_tooling(
    org_alias: str, query: str, timeout: int
) -> tuple[list[dict], str | None]:
    """Run one Tooling-API SOQL query. Returns (records, error_message_or_None).

    The records list is the raw `result.records` array from sf's --json output,
    so callers can read whichever fields the query selected.
    """
    cmd = [
        _sf_exe(), "data", "query",
        "-q", query,
        "--target-org", org_alias,
        "--use-tooling-api",
        "--json",
    ]
    logger.info("Tooling query: %s", query)

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return [], f"query timed out after {timeout}s"

    try:
        payload = json.loads(proc.stdout) if proc.stdout else {}
    except json.JSONDecodeError:
        return [], f"non-JSON response: {proc.stdout[-500:]!r}"

    if payload.get("status") != 0:
        return [], (
            f"query failed status={payload.get('status')} "
            f"name={payload.get('name')} message={payload.get('message')}"
        )

    return (payload.get("result") or {}).get("records") or [], None


def _reconstruct_object_api_name(
    developer_name: str | None, namespace_prefix: str | None
) -> str | None:
    """Rebuild the Salesforce-source api name from Tooling fields.

    `CustomObject.DeveloperName` is the api name without the `__c` suffix and
    without any managed-package namespace. So `Project_Task` becomes
    `Project_Task__c`, and a namespaced one becomes `acme__Project_Task__c`.
    """
    if not developer_name:
        return None
    base = f"{developer_name}__c"
    if namespace_prefix:
        return f"{namespace_prefix}__{base}"
    return base


def _fetch_custom_object_inventory(
    org_alias: str, timeout: int
) -> tuple[list[OrgComponent], list[str]]:
    """CustomObject inventory = max(object.LastModifiedDate, max field modified).

    Two queries:
      1. CustomObject — Id + DeveloperName + NamespacePrefix + LastModifiedDate.
      2. CustomField — GROUP BY TableEnumOrId, MAX(LastModifiedDate). Caught
         field-only changes that wouldn't bump the object's own row.

    Standard objects (Account, Contact, Lead, etc.) don't appear in
    `CustomObject` and are silently excluded — those need EntityDefinition
    or full-retrieve, deferred to a future slice.
    """
    components: list[OrgComponent] = []
    errors: list[str] = []

    object_records, err = _query_tooling(
        org_alias=org_alias,
        query=(
            "SELECT Id, DeveloperName, NamespacePrefix, LastModifiedDate "
            "FROM CustomObject"
        ),
        timeout=timeout,
    )
    if err:
        errors.append(f"CustomObject: {err}")
        return components, errors

    field_records, err = _query_tooling(
        org_alias=org_alias,
        query=(
            "SELECT TableEnumOrId, MAX(LastModifiedDate) lmd "
            "FROM CustomField "
            "GROUP BY TableEnumOrId"
        ),
        timeout=timeout,
    )
    if err:
        # Don't bail — proceed with object-only timestamps. Field-level edits
        # may be missed this run, but the next refresh will catch them.
        errors.append(f"CustomField aggregate: {err}")
        field_records = []

    field_max_by_parent_id: dict[str, str] = {}
    for rec in field_records:
        parent_id = rec.get("TableEnumOrId") or ""
        # Aggregate functions in Tooling SOQL alias to lowercase by default.
        lmd = rec.get("lmd") or rec.get("expr0") or ""
        if parent_id and lmd:
            field_max_by_parent_id[parent_id] = lmd

    for rec in object_records:
        api_name = _reconstruct_object_api_name(
            rec.get("DeveloperName"), rec.get("NamespacePrefix")
        )
        if not api_name:
            continue
        obj_lmd = rec.get("LastModifiedDate") or ""
        field_lmd = field_max_by_parent_id.get(rec.get("Id") or "", "")
        # ISO-8601 lex compare; either string may be empty.
        merged = max(obj_lmd, field_lmd)
        if not merged:
            continue
        components.append(OrgComponent(
            component_type="CustomObject",
            api_name=api_name,
            last_modified_at=merged,
        ))

    return components, errors


def fetch_org_inventory(
    org_alias: str,
    component_types: Iterable[str],
    timeout: int = 120,
) -> OrgInventory:
    """Pull (Id, Name, LastModifiedDate) for each requested type via the Tooling API."""
    inventory = OrgInventory(types_queried=[t for t in component_types])

    for ctype in component_types:
        if ctype == "CustomObject":
            comps, errs = _fetch_custom_object_inventory(org_alias, timeout)
            inventory.components.extend(comps)
            inventory.errors.extend(errs)
            continue

        query = _INVENTORY_QUERIES.get(ctype)
        if query is None:
            inventory.errors.append(
                f"No inventory query registered for {ctype}; skipping"
            )
            continue

        records, err = _query_tooling(org_alias, query, timeout)
        if err:
            inventory.errors.append(f"{ctype}: {err}")
            continue

        for rec in records:
            api_name = rec.get("Name")
            modified = rec.get("LastModifiedDate")
            if not api_name or not modified:
                continue
            inventory.components.append(OrgComponent(
                component_type=ctype,
                api_name=api_name,
                last_modified_at=modified,
            ))

    return inventory


# ---------------------------------------------------------------------------
# Delta computation
# ---------------------------------------------------------------------------

def compute_deltas(
    inventory: OrgInventory,
    indexed: dict[str, str],
    requested_types: Iterable[str],
) -> DeltaPlan:
    """Classify components into to_fetch / to_delete / unchanged.

    `indexed` maps component_id -> last_indexed_at (the timestamp WE recorded
    when the row was last upserted). The org's `last_modified_at` is when the
    component was last changed *in the org*. We re-fetch when:

        org.last_modified_at > indexed.last_indexed_at

    ISO-8601 strings are sortable, so we compare them lexicographically without
    parsing — both sides are guaranteed UTC-Z-suffixed in their respective
    sources.

    Components present in the index but absent from the org's inventory (and
    whose type was actually queried) are scheduled for deletion.

    Types not in SUPPORTED_DELTA_TYPES are surfaced via `unsupported_types`
    so the caller can full-fetch them via the existing path.
    """
    plan = DeltaPlan()

    requested = list(requested_types)
    plan.unsupported_types = [t for t in requested if t not in SUPPORTED_DELTA_TYPES]

    org_by_id = inventory.by_id()
    indexed_set = set(indexed.keys())

    # Pass 1: components present in the org -> fetch if new or changed.
    for cid, org_component in org_by_id.items():
        if cid not in indexed:
            plan.to_fetch.append(cid)
            continue
        # Both sides are ISO-8601 UTC; lexical compare is correct.
        if org_component.last_modified_at > indexed[cid]:
            plan.to_fetch.append(cid)
        else:
            plan.unchanged.append(cid)

    # Pass 2: components in the index, type was queried, but absent from the org -> delete.
    queried_types = set(inventory.types_queried) & SUPPORTED_DELTA_TYPES
    for cid in indexed_set - org_by_id.keys():
        # Only schedule deletion for component types we actually inventoried.
        # Otherwise we'd wipe rows we just didn't ask the org about.
        ctype = cid.split(":", 1)[0] if ":" in cid else ""
        if ctype in queried_types:
            plan.to_delete.append(cid)

    return plan
