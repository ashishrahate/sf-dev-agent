"""Parser for ValidationRule metadata files (*.validationRule-meta.xml).

In sfdx source format these live at
`objects/<ObjectName>/validationRules/<RuleName>.validationRule-meta.xml`.
The owning object is taken from the path (parent of `validationRules/`).

Top-level component with a `VALIDATES_ON` edge to the parent CustomObject —
deliberately not parented via `parent_id` because the file walk order is
filesystem-dependent and the FK check would fire before the parent
CustomObject is guaranteed to be upserted.
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

_SUFFIX = ".validationRule-meta.xml"


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _xml_to_dict(elem: ET.Element) -> dict:
    """Flatten one level of an XML element into {tag: text-or-dict}."""
    result: dict = {}
    for child in elem:
        key = _strip_ns(child.tag)
        if list(child):
            result[key] = _xml_to_dict(child)
        else:
            result[key] = (child.text or "").strip()
    return result


class ValidationRuleParser(Parser):
    component_type = "ValidationRule"

    def handles(self, path: Path) -> bool:
        return path.name.endswith(_SUFFIX)

    def parse(self, path: Path) -> ParseResult:
        rule_name = path.name.removesuffix(_SUFFIX)
        object_name = self._owning_object(path)
        component_id = self.make_id(
            "ValidationRule", rule_name, parent_api_name=object_name
        )

        source = path.read_text(encoding="utf-8", errors="replace")
        try:
            root = ET.fromstring(source)
            attrs = _xml_to_dict(root)
        except ET.ParseError as exc:
            return ParseResult(components=[ParsedComponent(
                id=component_id,
                component_type="ValidationRule",
                api_name=rule_name,
                file_path=str(path),
                source=source,
                metadata={
                    "object": object_name,
                    "parse_error": str(exc),
                },
            )])

        metadata = {
            "object": object_name,
            "active": attrs.get("active") == "true",
            "description": attrs.get("description") or None,
            "error_condition_formula": attrs.get("errorConditionFormula") or None,
            "error_message": attrs.get("errorMessage") or None,
            "error_display_field": attrs.get("errorDisplayField") or None,
        }

        component = ParsedComponent(
            id=component_id,
            component_type="ValidationRule",
            api_name=rule_name,
            file_path=str(path),
            source=source,
            metadata=metadata,
        )

        relationships: list[ParsedRelationship] = []
        if object_name:
            relationships.append(ParsedRelationship(
                source_id=component_id,
                target_id=self.make_id("CustomObject", object_name),
                relationship_type="VALIDATES_ON",
            ))

        return ParseResult(components=[component], relationships=relationships)

    @staticmethod
    def _owning_object(path: Path) -> str:
        """Extract the parent object's API name from the file path.

        Expected layout: `objects/<ObjectName>/validationRules/<rule>.validationRule-meta.xml`.
        Falls back to "" when the path doesn't match — parser still emits the
        component (just without a relationship), so a misplaced file doesn't
        kill the run.
        """
        parents = path.parents
        if len(parents) >= 2 and parents[0].name == "validationRules":
            return parents[1].name
        return ""


register(ValidationRuleParser())
