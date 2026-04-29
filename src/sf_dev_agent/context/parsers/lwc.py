"""Parser for Lightning Web Component bundles.

LWC components are bundle-shaped — a directory under `lwc/<bundleName>/`
containing at minimum `<bundleName>.js`, `<bundleName>.html`, and
`<bundleName>.js-meta.xml`. Optionally `<bundleName>.css`, SVG icons, and
additional helper files.

The dispatcher hits each file individually, so this parser triggers off the
`*.js-meta.xml` marker (unambiguous for LWC — Aura uses `.cmp-meta.xml`,
fields use `.field-meta.xml`). On each match it reads the sibling `.js`
controller for `@salesforce/apex/...` imports and emits one
`LightningComponentBundle` component plus REFERENCES edges to the named
ApexClasses.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from sf_dev_agent.context.parsers.base import (
    ParsedComponent,
    ParsedRelationship,
    ParseResult,
    Parser,
    register,
)

_SUFFIX = ".js-meta.xml"

# Matches both:
#   import getAccount from '@salesforce/apex/AccountController.getAccount';
#   import { x } from "@salesforce/apex/AccountController.getX"
# Captures the ApexClass name (group 1).
_APEX_IMPORT = re.compile(
    r"""['"]@salesforce/apex/(?P<cls>[A-Za-z_][\w]*)\.[A-Za-z_][\w]*['"]"""
)

# import { NAME, ... } from '@salesforce/schema/<Object>[.<Field>]'
# Captures the object (group 1) and an optional field segment (group 2).
_SCHEMA_IMPORT = re.compile(
    r"""['"]@salesforce/schema/(?P<obj>[A-Za-z_][\w]*)(?:\.(?P<field>[A-Za-z_][\w]*))?['"]"""
)


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _child_text(elem: ET.Element, name: str) -> str | None:
    for c in elem:
        if _strip_ns(c.tag) == name:
            return (c.text or "").strip() or None
    return None


def _children(elem: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in elem if _strip_ns(c.tag) == name]


class LWCParser(Parser):
    component_type = "LightningComponentBundle"

    def handles(self, path: Path) -> bool:
        # Require the bundle structure: parent dir named after the file's stem.
        if not path.name.endswith(_SUFFIX):
            return False
        bundle_name = path.name.removesuffix(_SUFFIX)
        return path.parent.name == bundle_name

    def parse(self, path: Path) -> ParseResult:
        bundle_name = path.name.removesuffix(_SUFFIX)
        bundle_dir = path.parent
        component_id = self.make_id("LightningComponentBundle", bundle_name)

        meta_source = path.read_text(encoding="utf-8", errors="replace")
        api_version = is_exposed = master_label = description = None
        targets: list[str] = []
        try:
            root = ET.fromstring(meta_source)
            api_version = _child_text(root, "apiVersion")
            raw_exposed = _child_text(root, "isExposed")
            is_exposed = raw_exposed == "true" if raw_exposed is not None else None
            master_label = _child_text(root, "masterLabel")
            description = _child_text(root, "description")
            for targets_elem in _children(root, "targets"):
                for t in _children(targets_elem, "target"):
                    if t.text:
                        targets.append(t.text.strip())
        except ET.ParseError:
            # Keep going — we still emit the component with whatever sibling
            # files we can read.
            pass

        # --- Sibling .js controller -------------------------------------------
        js_path = bundle_dir / f"{bundle_name}.js"
        js_source = ""
        apex_imports: list[str] = []
        schema_objects: set[str] = set()
        schema_fields: set[tuple[str, str]] = set()
        if js_path.is_file():
            js_source = js_path.read_text(encoding="utf-8", errors="replace")
            apex_imports = sorted({m.group("cls") for m in _APEX_IMPORT.finditer(js_source)})
            for m in _SCHEMA_IMPORT.finditer(js_source):
                obj = m.group("obj")
                field = m.group("field")
                schema_objects.add(obj)
                if field:
                    schema_fields.add((obj, field))

        # --- Sibling .html template -------------------------------------------
        html_path = bundle_dir / f"{bundle_name}.html"
        html_source = html_path.read_text(encoding="utf-8", errors="replace") \
            if html_path.is_file() else ""

        # --- Sibling .css (optional) ------------------------------------------
        css_path = bundle_dir / f"{bundle_name}.css"
        has_css = css_path.is_file()

        # Concatenated source — what the embedder treats as the "document".
        # Markers help retrieval surface the right slice.
        combined = f"// {bundle_name}.js-meta.xml\n{meta_source}\n"
        if js_source:
            combined += f"\n// {bundle_name}.js\n{js_source}\n"
        if html_source:
            combined += f"\n<!-- {bundle_name}.html -->\n{html_source}\n"

        metadata = {
            "api_version": api_version,
            "is_exposed": is_exposed,
            "master_label": master_label,
            "description": description,
            "targets": targets,
            "has_html": bool(html_source),
            "has_css": has_css,
            "apex_imports": apex_imports,
            "schema_objects": sorted(schema_objects),
            "schema_fields": sorted(f"{o}.{f}" for o, f in schema_fields),
        }

        component = ParsedComponent(
            id=component_id,
            component_type="LightningComponentBundle",
            api_name=bundle_name,
            file_path=str(bundle_dir),
            source=combined,
            metadata=metadata,
        )

        relationships: list[ParsedRelationship] = []
        for cls in apex_imports:
            relationships.append(ParsedRelationship(
                source_id=component_id,
                target_id=self.make_id("ApexClass", cls),
                relationship_type="REFERENCES",
            ))
        for obj in sorted(schema_objects):
            relationships.append(ParsedRelationship(
                source_id=component_id,
                target_id=self.make_id("CustomObject", obj),
                relationship_type="REFERENCES_OBJECT",
            ))

        return ParseResult(components=[component], relationships=relationships)


register(LWCParser())
