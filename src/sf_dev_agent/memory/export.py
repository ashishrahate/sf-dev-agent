"""Markdown export for the memory store.

Wave 8 slice 3b. SQLite is the canonical store; this module produces a
human-readable Markdown view so users can:

    - inspect what's stored without running SQL,
    - `git add` the export dir to version their saved memories,
    - back up / move memories between machines via a checked-in file,
    - share institutional knowledge with a teammate via a PR.

Format mirrors `context/knowledge/entries/<category>/*.md` — YAML
frontmatter at the top with every column needed to round-trip a row,
then the body as Markdown:

    ---
    id: t1:OrgA:dup-detection-pref:abcd1234
    tenant_id: t1
    org_alias: OrgA
    type: feedback
    name: dup-detection-pref
    description: prefer Email__c + Phone match for account dedup
    tags: [dedup, accounts]
    source_session_id: task_20260427120000
    created_at: 2026-04-27T12:00:00+00:00
    last_accessed_at: 2026-04-27T12:00:00+00:00
    access_count: 0
    superseded_by: null
    ---
    Rule: when detecting duplicate accounts, match on Email__c + Phone.

    **Why:** prior incident where bare-name match merged unrelated tenants.
    **How to apply:** any new dedup logic on Account.

Default export dir is `.cache/memory/exports/` (gitignored). Pass an
explicit `--out <dir>` from the CLI to land files somewhere commit-ready.

Public API:
    MemoryExporter(store, out_dir=None)
        .export(scope, type=None, include_superseded=False) -> ExportResult

    ExportResult(files: list[Path], skipped: int)
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from sf_dev_agent.memory.store import MemoryRecord, MemoryScope, MemoryStore, _slugify
from sf_dev_agent.paths import repo_root

logger = logging.getLogger(__name__)


@dataclass
class ExportResult:
    files: list[Path] = field(default_factory=list)
    skipped: int = 0


def default_export_dir() -> Path:
    """Repo-relative export dir. Gitignored by default."""
    return repo_root() / ".cache" / "memory" / "exports"


class MemoryExporter:
    """Dumps `memories` rows to per-row Markdown files."""

    def __init__(
        self,
        store: MemoryStore,
        out_dir: Path | str | None = None,
    ) -> None:
        self._store = store
        self.out_dir = Path(out_dir) if out_dir else default_export_dir()

    def export(
        self,
        scope: MemoryScope,
        type: str | None = None,
        include_superseded: bool = False,
        limit: int = 1000,
    ) -> ExportResult:
        """Walk memories in scope and write one Markdown file per row.

        File naming: `<type>__<slug>__<short-id>.md`. The short-id (last
        segment of `id`) prevents collisions when two memories happen to
        share a slug.
        """
        records = self._store.list(
            scope=scope,
            type=type,
            include_superseded=include_superseded,
            limit=limit,
        )
        if not records:
            return ExportResult()

        self.out_dir.mkdir(parents=True, exist_ok=True)
        result = ExportResult()
        for record in records:
            try:
                path = self._write_record(record)
                result.files.append(path)
            except OSError as exc:
                logger.warning("Failed to write %s: %s", record.id, exc)
                result.skipped += 1

        return result

    def _write_record(self, record: MemoryRecord) -> Path:
        slug = _slugify(record.name)
        short = record.id.rsplit(":", 1)[-1]
        filename = f"{record.type}__{slug}__{short}.md"
        path = self.out_dir / filename
        path.write_text(_render_markdown(record), encoding="utf-8")
        return path


# ---------------------------------------------------------------------------
# Frontmatter rendering
# ---------------------------------------------------------------------------

# Characters that force YAML-style quoting when emitting frontmatter values.
# We don't pull in a full YAML library — the dialect we emit is the same one
# the knowledge-base parser already accepts.
_NEEDS_QUOTING_RE = re.compile(r"""[:#&*!|>'\"%@`,\[\]\{\}\n]""")


def _render_markdown(record: MemoryRecord) -> str:
    lines = ["---"]
    lines.append(f"id: {_yaml_scalar(record.id)}")
    lines.append(f"tenant_id: {_yaml_scalar(record.tenant_id)}")
    lines.append(f"org_alias: {_yaml_optional(record.org_alias)}")
    lines.append(f"type: {record.type}")
    lines.append(f"name: {_yaml_scalar(record.name)}")
    lines.append(f"description: {_yaml_scalar(record.description)}")
    lines.append(f"tags: {_yaml_list(record.tags)}")
    lines.append(f"source_session_id: {_yaml_optional(record.source_session_id)}")
    lines.append(f"created_at: {record.created_at}")
    lines.append(f"last_accessed_at: {record.last_accessed_at}")
    lines.append(f"access_count: {record.access_count}")
    lines.append(f"superseded_by: {_yaml_optional(record.superseded_by)}")
    lines.append("---")
    lines.append("")
    lines.append(record.body.rstrip())
    lines.append("")
    return "\n".join(lines)


def _yaml_scalar(value: str) -> str:
    """Quote-if-needed string scalar for our minimal YAML dialect."""
    if value == "":
        return '""'
    if _NEEDS_QUOTING_RE.search(value):
        # JSON-encode — produces valid YAML for double-quoted scalars and
        # handles all the escaping (newlines, backslashes, quotes) for us.
        return json.dumps(value)
    return value


def _yaml_optional(value: str | None) -> str:
    if value is None:
        return "null"
    return _yaml_scalar(value)


def _yaml_list(items: list[str]) -> str:
    if not items:
        return "[]"
    # Inline list form — knowledge_base's _parse_inline_value handles this.
    return "[" + ", ".join(_yaml_scalar(item) for item in items) + "]"
