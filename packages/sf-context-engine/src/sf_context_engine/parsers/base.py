"""Parser ABC + result dataclasses.

A `Parser` decides whether it handles a given file, then yields one or more
`ParsedComponent` records (and optionally `ParsedRelationship` edges) for the
metadata index to upsert.

Adding a new metadata type — ValidationRule, Flow, CustomMetadataType, LWC —
means writing a new Parser subclass and registering it. No schema changes,
no edits to the index core, no edits to the orchestrator.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


@dataclass
class ParsedComponent:
    """One row destined for the `components` table."""
    id: str                                    # "ApexClass:AccountHandler"
    component_type: str                        # "ApexClass"
    api_name: str                              # "AccountHandler"
    parent_id: str | None = None               # e.g. CustomField -> CustomObject:Account
    file_path: str | None = None               # relative path inside retrieve dir
    source: str | None = None                  # raw file content
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParsedRelationship:
    """One row destined for the `relationships` table."""
    source_id: str
    target_id: str
    relationship_type: str                     # "TRIGGERS_ON", "REFERENCES", "EXTENDS", ...
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParseResult:
    components: list[ParsedComponent] = field(default_factory=list)
    relationships: list[ParsedRelationship] = field(default_factory=list)


class Parser(ABC):
    """Base class for metadata parsers.

    Subclasses declare `component_type` (used by the orchestrator for logging
    and selective ingestion) and implement `handles` + `parse`.
    """

    component_type: str = ""  # subclasses override

    @abstractmethod
    def handles(self, path: Path) -> bool:
        """Return True if this parser knows how to read the given file."""

    @abstractmethod
    def parse(self, path: Path) -> ParseResult:
        """Read the file and yield component + relationship records."""

    @staticmethod
    def make_id(component_type: str, api_name: str, parent_api_name: str | None = None) -> str:
        """Canonical component ID format used as the SQLite primary key."""
        if parent_api_name:
            return f"{component_type}:{parent_api_name}.{api_name}"
        return f"{component_type}:{api_name}"


# ---------------------------------------------------------------------------
# Module-level registry
# ---------------------------------------------------------------------------

_REGISTRY: list[Parser] = []


def register(parser: Parser) -> Parser:
    """Register a parser instance. Called at import time by each parser module."""
    _REGISTRY.append(parser)
    return parser


def get_parsers() -> list[Parser]:
    """Return all registered parsers (orchestrator iterates this list)."""
    return list(_REGISTRY)


def dispatch(path: Path) -> Parser | None:
    """Find the first registered parser that handles `path`, or None."""
    for parser in _REGISTRY:
        if parser.handles(path):
            return parser
    return None


def _reset_for_tests() -> None:
    """Test helper: clear the registry so a test can register a stub parser."""
    _REGISTRY.clear()


def discovered_component_types() -> Iterable[str]:
    """Yield the component_type string for each registered parser."""
    return [p.component_type for p in _REGISTRY if p.component_type]
