"""Parser for CustomObject metadata files (*.object-meta.xml).

In source format (sfdx) the object's *.object-meta.xml lives at
`objects/<ApiName>/<ApiName>.object-meta.xml`. Field metadata is stored as
separate `*.field-meta.xml` files alongside it; this parser walks those
siblings and emits one CustomField component per file, parented to the object.

ValidationRule, RecordType, ListView, etc. live in their own sibling
directories — those are handled by their own parsers (future work).
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

_NS = {"sf": "http://soap.sforce.com/2006/04/metadata"}


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _xml_to_dict(elem: ET.Element) -> dict:
    """Flatten a shallow XML element into a dict of {tag: text-or-list}."""
    result: dict = {}
    for child in elem:
        key = _strip_ns(child.tag)
        value: object
        if list(child):
            value = _xml_to_dict(child)
        else:
            value = (child.text or "").strip()
        # Repeated tags collapse into a list.
        if key in result:
            existing = result[key]
            if isinstance(existing, list):
                existing.append(value)
            else:
                result[key] = [existing, value]
        else:
            result[key] = value
    return result


class CustomObjectParser(Parser):
    component_type = "CustomObject"

    def handles(self, path: Path) -> bool:
        return path.name.endswith(".object-meta.xml")

    def parse(self, path: Path) -> ParseResult:
        source = path.read_text(encoding="utf-8", errors="replace")
        try:
            root = ET.fromstring(source)
        except ET.ParseError as exc:
            api_name = path.name.removesuffix(".object-meta.xml")
            return ParseResult(components=[ParsedComponent(
                id=self.make_id("CustomObject", api_name),
                component_type="CustomObject",
                api_name=api_name,
                file_path=str(path),
                source=source,
                metadata={"parse_error": str(exc)},
            )])

        # In sfdx source format the object's API name comes from the file name,
        # not an XML element (the file is named after the object).
        api_name = path.name.removesuffix(".object-meta.xml")
        object_id = self.make_id("CustomObject", api_name)

        object_attrs = _xml_to_dict(root)
        # Drop verbose nested arrays we won't query — they live in `source` if needed.
        slim = {
            k: v for k, v in object_attrs.items()
            if k in {"label", "pluralLabel", "sharingModel", "deploymentStatus",
                     "description", "enableActivities", "enableHistory", "enableReports"}
        }

        components: list[ParsedComponent] = [ParsedComponent(
            id=object_id,
            component_type="CustomObject",
            api_name=api_name,
            file_path=str(path),
            source=source,
            metadata=slim,
        )]
        relationships: list[ParsedRelationship] = []

        # Walk sibling fields/ directory for separate field-meta.xml files.
        fields_dir = path.parent / "fields"
        if fields_dir.is_dir():
            for field_file in sorted(fields_dir.glob("*.field-meta.xml")):
                field_component = self._parse_field(field_file, object_api_name=api_name)
                if field_component:
                    components.append(field_component)
                    relationships.append(ParsedRelationship(
                        source_id=field_component.id,
                        target_id=object_id,
                        relationship_type="FIELD_OF",
                    ))

        return ParseResult(components=components, relationships=relationships)

    def _parse_field(
        self, field_file: Path, object_api_name: str
    ) -> ParsedComponent | None:
        field_source = field_file.read_text(encoding="utf-8", errors="replace")
        field_api = field_file.name.removesuffix(".field-meta.xml")
        try:
            field_root = ET.fromstring(field_source)
        except ET.ParseError:
            return ParsedComponent(
                id=self.make_id("CustomField", field_api, parent_api_name=object_api_name),
                component_type="CustomField",
                api_name=field_api,
                parent_id=self.make_id("CustomObject", object_api_name),
                file_path=str(field_file),
                source=field_source,
                metadata={"parse_error": "xml parse failed"},
            )

        attrs = _xml_to_dict(field_root)
        return ParsedComponent(
            id=self.make_id("CustomField", field_api, parent_api_name=object_api_name),
            component_type="CustomField",
            api_name=field_api,
            parent_id=self.make_id("CustomObject", object_api_name),
            file_path=str(field_file),
            source=field_source,
            metadata={
                "label": attrs.get("label"),
                "type": attrs.get("type"),
                "required": attrs.get("required") == "true",
                "unique": attrs.get("unique") == "true",
                "external_id": attrs.get("externalId") == "true",
                "reference_to": attrs.get("referenceTo"),
                "formula": attrs.get("formula"),
                "length": attrs.get("length"),
                "precision": attrs.get("precision"),
                "scale": attrs.get("scale"),
            },
        )


register(CustomObjectParser())
