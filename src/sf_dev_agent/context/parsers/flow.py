"""Parser for Flow metadata files (*.flow-meta.xml).

Flows live at `flows/<FlowName>.flow-meta.xml` in sfdx source format. They are
top-level components — no parent. The interesting structure for retrieval and
graph queries:

- `start.object` for record-triggered flows → `TRIGGERS_ON` edge to the
  CustomObject (same shape as ApexTrigger).
- `actionCalls` with `actionType=apex` → `REFERENCES` edge to the named
  ApexClass (these classes implement @InvocableMethod).
- `recordCreates` / `recordUpdates` / `recordLookups` / `recordDeletes`
  → `REFERENCES_OBJECT` edges to each sObject the flow reads or writes.
- `subflows` with `flowName` → `REFERENCES_FLOW` edges to other flows.

Top-level metadata (label, processType, status, start_object, counts of each
element kind) lives in `metadata` for cheap searching and inspection.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from sf_dev_agent.context.parsers.base import (
    ParsedComponent,
    ParsedRelationship,
    ParseResult,
    Parser,
    register,
)

_SUFFIX = ".flow-meta.xml"
_NS = "http://soap.sforce.com/2006/04/metadata"


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _children(elem: ET.Element, name: str) -> list[ET.Element]:
    """Direct children whose stripped tag matches `name`."""
    return [c for c in elem if _strip_ns(c.tag) == name]


def _child_text(elem: ET.Element, name: str) -> str | None:
    for c in elem:
        if _strip_ns(c.tag) == name:
            return (c.text or "").strip() or None
    return None


class FlowParser(Parser):
    component_type = "Flow"

    def handles(self, path: Path) -> bool:
        return path.name.endswith(_SUFFIX)

    def parse(self, path: Path) -> ParseResult:
        flow_name = path.name.removesuffix(_SUFFIX)
        component_id = self.make_id("Flow", flow_name)
        source = path.read_text(encoding="utf-8", errors="replace")

        try:
            root = ET.fromstring(source)
        except ET.ParseError as exc:
            return ParseResult(components=[ParsedComponent(
                id=component_id,
                component_type="Flow",
                api_name=flow_name,
                file_path=str(path),
                source=source,
                metadata={"parse_error": str(exc)},
            )])

        # --- Top-level scalars -------------------------------------------------
        label = _child_text(root, "label")
        process_type = _child_text(root, "processType")
        status = _child_text(root, "status")
        interview_label = _child_text(root, "interviewLabel")

        # --- Start block (record-trigger metadata) -----------------------------
        start_elements = _children(root, "start")
        start_block = start_elements[0] if start_elements else None
        start_object = _child_text(start_block, "object") if start_block is not None else None
        record_trigger_type = (
            _child_text(start_block, "recordTriggerType") if start_block is not None else None
        )
        trigger_type = (
            _child_text(start_block, "triggerType") if start_block is not None else None
        )

        # --- Apex invocable actions -------------------------------------------
        apex_action_classes: list[str] = []
        for action in _children(root, "actionCalls"):
            if _child_text(action, "actionType") == "apex":
                action_name = _child_text(action, "actionName")
                if action_name:
                    apex_action_classes.append(action_name)

        # --- Record-touching elements -----------------------------------------
        record_objects: set[str] = set()
        for kind in ("recordCreates", "recordUpdates",
                     "recordLookups", "recordDeletes"):
            for elem in _children(root, kind):
                obj = _child_text(elem, "object")
                if obj:
                    record_objects.add(obj)

        # --- Subflows ----------------------------------------------------------
        subflow_names: list[str] = []
        for sub in _children(root, "subflows"):
            sub_name = _child_text(sub, "flowName")
            if sub_name:
                subflow_names.append(sub_name)

        metadata = {
            "label": label,
            "process_type": process_type,
            "status": status,
            "interview_label": interview_label,
            "start_object": start_object,
            "record_trigger_type": record_trigger_type,
            "trigger_type": trigger_type,
            "apex_action_classes": sorted(set(apex_action_classes)),
            "record_objects": sorted(record_objects),
            "subflows": sorted(set(subflow_names)),
        }

        component = ParsedComponent(
            id=component_id,
            component_type="Flow",
            api_name=flow_name,
            file_path=str(path),
            source=source,
            metadata=metadata,
        )

        relationships: list[ParsedRelationship] = []
        if start_object:
            relationships.append(ParsedRelationship(
                source_id=component_id,
                target_id=self.make_id("CustomObject", start_object),
                relationship_type="TRIGGERS_ON",
                metadata={
                    "record_trigger_type": record_trigger_type,
                    "trigger_type": trigger_type,
                },
            ))

        for class_name in sorted(set(apex_action_classes)):
            relationships.append(ParsedRelationship(
                source_id=component_id,
                target_id=self.make_id("ApexClass", class_name),
                relationship_type="REFERENCES",
            ))

        # Skip the start_object here — it's already captured as TRIGGERS_ON.
        for obj in sorted(record_objects):
            if obj == start_object:
                continue
            relationships.append(ParsedRelationship(
                source_id=component_id,
                target_id=self.make_id("CustomObject", obj),
                relationship_type="REFERENCES_OBJECT",
            ))

        for sub_name in sorted(set(subflow_names)):
            relationships.append(ParsedRelationship(
                source_id=component_id,
                target_id=self.make_id("Flow", sub_name),
                relationship_type="REFERENCES_FLOW",
            ))

        return ParseResult(components=[component], relationships=relationships)


register(FlowParser())
