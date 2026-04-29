"""Parser for RecordType metadata files (*.recordType-meta.xml).

In sfdx source format these live at
`objects/<ObjectName>/recordTypes/<TypeName>.recordType-meta.xml`.
The owning object comes from the path (parent of `recordTypes/`).

Top-level component with a `RECORD_TYPE_OF` edge to the parent CustomObject.
Same FK-safe shape as ValidationRule — no `parent_id` because file walk order
is filesystem-dependent.
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

_SUFFIX = ".recordType-meta.xml"


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


class RecordTypeParser(Parser):
    component_type = "RecordType"

    def handles(self, path: Path) -> bool:
        return path.name.endswith(_SUFFIX)

    def parse(self, path: Path) -> ParseResult:
        type_name = path.name.removesuffix(_SUFFIX)
        object_name = self._owning_object(path)
        component_id = self.make_id(
            "RecordType", type_name, parent_api_name=object_name
        )

        source = path.read_text(encoding="utf-8", errors="replace")
        try:
            root = ET.fromstring(source)
            attrs = _xml_to_dict(root)
        except ET.ParseError as exc:
            return ParseResult(components=[ParsedComponent(
                id=component_id,
                component_type="RecordType",
                api_name=type_name,
                file_path=str(path),
                source=source,
                metadata={
                    "object": object_name,
                    "parse_error": str(exc),
                },
            )])

        metadata = {
            "object": object_name,
            "label": attrs.get("label") or None,
            "active": attrs.get("active") == "true",
            "description": attrs.get("description") or None,
            "business_process": attrs.get("businessProcess") or None,
        }

        component = ParsedComponent(
            id=component_id,
            component_type="RecordType",
            api_name=type_name,
            file_path=str(path),
            source=source,
            metadata=metadata,
        )

        relationships: list[ParsedRelationship] = []
        if object_name:
            relationships.append(ParsedRelationship(
                source_id=component_id,
                target_id=self.make_id("CustomObject", object_name),
                relationship_type="RECORD_TYPE_OF",
            ))

        return ParseResult(components=[component], relationships=relationships)

    @staticmethod
    def _owning_object(path: Path) -> str:
        """Extract the parent object's API name from the file path.

        Expected: `objects/<ObjectName>/recordTypes/<type>.recordType-meta.xml`.
        Returns "" when the layout doesn't match — parser still emits the
        component without a relationship.
        """
        parents = path.parents
        if len(parents) >= 2 and parents[0].name == "recordTypes":
            return parents[1].name
        return ""


register(RecordTypeParser())
