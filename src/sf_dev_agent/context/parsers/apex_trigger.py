"""Parser for ApexTrigger source files (*.trigger).

Extracts the trigger's target object and its event list. Emits a TRIGGERS_ON
relationship — the orchestrator drops it later if the target object isn't in
the index (e.g. standard sObjects we didn't ingest).
"""

from __future__ import annotations

import re
from pathlib import Path

from sf_dev_agent.context.parsers.base import (
    ParsedComponent,
    ParsedRelationship,
    ParseResult,
    Parser,
    register,
)

_TRIGGER_DECL = re.compile(
    r"""
    \btrigger\s+(?P<name>[A-Za-z_][\w]*)
    \s+on\s+(?P<object>[A-Za-z_][\w]*)
    \s*\(\s*(?P<events>[^)]+)\)
    """,
    re.IGNORECASE | re.VERBOSE,
)


class ApexTriggerParser(Parser):
    component_type = "ApexTrigger"

    def handles(self, path: Path) -> bool:
        return path.suffix.lower() == ".trigger"

    def parse(self, path: Path) -> ParseResult:
        source = path.read_text(encoding="utf-8", errors="replace")

        match = _TRIGGER_DECL.search(source)
        if not match:
            # Malformed or unparseable — store source so the agent can still read it.
            return ParseResult(components=[ParsedComponent(
                id=self.make_id("ApexTrigger", path.stem),
                component_type="ApexTrigger",
                api_name=path.stem,
                file_path=str(path),
                source=source,
                metadata={"parse_error": "could not extract trigger declaration"},
            )])

        api_name = match.group("name")
        target_object = match.group("object")
        events = [e.strip().lower() for e in match.group("events").split(",") if e.strip()]

        component_id = self.make_id("ApexTrigger", api_name)
        component = ParsedComponent(
            id=component_id,
            component_type="ApexTrigger",
            api_name=api_name,
            file_path=str(path),
            source=source,
            metadata={
                "target_object": target_object,
                "events": events,
                "line_count": source.count("\n") + 1,
            },
        )

        relationship = ParsedRelationship(
            source_id=component_id,
            target_id=self.make_id("CustomObject", target_object),
            relationship_type="TRIGGERS_ON",
            metadata={"events": events},
        )

        return ParseResult(components=[component], relationships=[relationship])


register(ApexTriggerParser())
