"""Parser for ApexClass source files (*.cls).

Lightweight regex extraction — class name, sharing model, parent class,
implemented interfaces, and a test-class flag. A future enhancement is to
swap this for a real Apex AST.
"""

from __future__ import annotations

import re
from pathlib import Path

from sf_dev_agent.context.parsers._apex_refs import extract_class_references
from sf_dev_agent.context.parsers.base import (
    ParsedComponent,
    ParsedRelationship,
    ParseResult,
    Parser,
    register,
)

_CLASS_DECL = re.compile(
    r"""
    (?P<sharing>(?:with|without|inherited)\s+sharing\s+)?
    \bclass\s+(?P<name>[A-Za-z_][\w]*)
    (?:\s+extends\s+(?P<parent>[A-Za-z_][\w.<>]*))?       # generics allowed
    (?:\s+implements\s+(?P<interfaces>[^{]+?))?           # consume up to opening brace
    \s*\{
    """,
    re.IGNORECASE | re.VERBOSE,
)

_IS_TEST = re.compile(r"@isTest\b", re.IGNORECASE)


class ApexClassParser(Parser):
    component_type = "ApexClass"

    def handles(self, path: Path) -> bool:
        return path.suffix.lower() == ".cls"

    def parse(self, path: Path) -> ParseResult:
        source = path.read_text(encoding="utf-8", errors="replace")
        # Strip line-comments and block-comments to keep the regex from latching onto
        # commented-out class declarations.
        stripped = re.sub(r"//[^\n]*", "", source)
        stripped = re.sub(r"/\*.*?\*/", "", stripped, flags=re.DOTALL)

        match = _CLASS_DECL.search(stripped)
        api_name = match.group("name") if match else path.stem
        sharing = (match.group("sharing") or "").strip().lower() if match else ""
        parent = match.group("parent") if match else None
        interfaces_raw = match.group("interfaces") if match else None

        interfaces = [
            part.strip()
            for part in (interfaces_raw or "").split(",")
            if part.strip()
        ]

        component_id = self.make_id("ApexClass", api_name)

        # Class references — used for REFERENCES edges and stored as metadata.
        # Suppress self-reference and the parent/interface names already covered
        # by EXTENDS / IMPLEMENTS edges.
        suppress = {api_name}
        if parent:
            suppress.add(parent)
        suppress.update(interfaces)
        references = sorted(extract_class_references(source, exclude=suppress))

        metadata = {
            "sharing_model": sharing or None,
            "extends": parent,
            "implements": interfaces,
            "is_test": bool(_IS_TEST.search(source)),
            "line_count": source.count("\n") + 1,
            "references": references,
        }

        component = ParsedComponent(
            id=component_id,
            component_type="ApexClass",
            api_name=api_name,
            file_path=str(path),
            source=source,
            metadata=metadata,
        )

        relationships: list[ParsedRelationship] = []
        if parent:
            relationships.append(ParsedRelationship(
                source_id=component_id,
                target_id=self.make_id("ApexClass", parent),
                relationship_type="EXTENDS",
            ))
        for iface in interfaces:
            relationships.append(ParsedRelationship(
                source_id=component_id,
                target_id=self.make_id("ApexClass", iface),
                relationship_type="IMPLEMENTS",
            ))
        for ref in references:
            relationships.append(ParsedRelationship(
                source_id=component_id,
                target_id=self.make_id("ApexClass", ref),
                relationship_type="REFERENCES",
            ))

        return ParseResult(components=[component], relationships=relationships)


register(ApexClassParser())
