"""Promote a project memory to a tenant-agnostic knowledge entry.

Wave 8 slice 3c. The third tier from PROJECT_SUMMARY: discoveries that
generalize beyond the tenant they were learned in get a `.md` draft
under `context/knowledge/entries/<category>/`. After review, the user
commits the draft to ship it as bundled platform knowledge for every
future agent run for every tenant.

Why this exists, briefly:
    Project memory is private to a (tenant, org). When the *insight* is
    universal — "Salesforce throws ApexClass body limit at 1MB" — keeping
    it locked inside one tenant's memories is wasted leverage. Promotion
    is the path from private learning to product-wide knowledge.

Key design calls:
    - **Manual gate.** Promotion is never automatic. Tenant-specific
      content (customer names, instance URLs, "Org A") must NOT leak into
      the cross-tenant pool. The flow produces a draft file; the user
      reviews and commits.
    - **Heuristic flag for tenant-specific content.** We scan the memory
      for org aliases / instance URLs / customer-name patterns and
      surface a warning at the top of the draft. NOT a hard block — the
      user has final say.
    - **`tenant_id` and `org_alias` are dropped.** A knowledge entry has
      no scope columns; a promoted memory must be re-framed as universal
      Salesforce knowledge, not "what we did at customer X."

Public API:
    MemoryPromoter(store, entries_dir=None)
        .promote(memory_id, category, severity=None,
                 tags=None, title=None, force=False) -> PromotionResult

    PromotionResult(file: Path, warnings: list[str], skipped: bool)

Categories must match the knowledge_base taxonomy:
    governor_limit | anti_pattern | best_practice | pattern
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from sf_dev_agent.memory.export import _yaml_list, _yaml_optional, _yaml_scalar
from sf_dev_agent.memory.store import MemoryStore, _slugify

# context.knowledge is imported lazily — it transitively pulls in
# context/__init__.py (loads orchestrator, which imports memory). Keeping
# the import inside __init__ defends against circular-import order when
# a CLI loads memory.promote before any context module.

logger = logging.getLogger(__name__)


# Knowledge-base taxonomy — must stay in lockstep with `_layer_knowledge`
# and the registered tool's `category` enum. Any drift here would land
# orphan entries on disk that the indexer never picks up.
KNOWLEDGE_CATEGORIES: frozenset[str] = frozenset({
    "governor_limit", "anti_pattern", "best_practice", "pattern",
})

KNOWLEDGE_SEVERITIES: frozenset[str] = frozenset({
    "critical", "high", "medium", "low", "info",
})


@dataclass
class PromotionResult:
    file: Path
    warnings: list[str] = field(default_factory=list)
    skipped: bool = False


class MemoryPromoter:
    """Drafts a knowledge_base entry from a project memory."""

    def __init__(
        self,
        store: MemoryStore,
        entries_dir: Path | str | None = None,
    ) -> None:
        self._store = store
        if entries_dir is None:
            from sf_dev_agent.context.knowledge import bundled_entries_dir
            entries_dir = bundled_entries_dir()
        self.entries_dir = Path(entries_dir)

    def promote(
        self,
        memory_id: str,
        category: str,
        severity: str | None = None,
        tags: list[str] | None = None,
        title: str | None = None,
        references: list[str] | None = None,
        force: bool = False,
    ) -> PromotionResult:
        """Write a draft `.md` to `entries/<category>/<slug>.md`.

        Args:
            memory_id: id of the project memory to promote.
            category: must be one of `KNOWLEDGE_CATEGORIES`.
            severity: optional; one of `KNOWLEDGE_SEVERITIES`.
            tags: list of strings; if None, copies the source memory's tags.
            title: human title for the entry; defaults to the memory's
                description (the one-line hook).
            references: list of URLs to attach to the entry. Optional.
            force: skip the tenant-specific-content heuristic warning gate.
                Even with force, warnings are still listed in the result.

        Returns:
            PromotionResult with the path written, any warnings the
            heuristics fired, and `skipped=True` if a hard block triggered.

        Raises:
            ValueError: missing memory, unknown category/severity, or
                invalid input.
            FileExistsError: target file already exists (refuse to clobber).
        """
        if category not in KNOWLEDGE_CATEGORIES:
            raise ValueError(
                f"unknown category {category!r}; must be one of "
                f"{sorted(KNOWLEDGE_CATEGORIES)}"
            )
        if severity is not None and severity not in KNOWLEDGE_SEVERITIES:
            raise ValueError(
                f"unknown severity {severity!r}; must be one of "
                f"{sorted(KNOWLEDGE_SEVERITIES)}"
            )

        memory = self._store.find_by_id(memory_id)
        if memory is None:
            raise ValueError(f"memory {memory_id!r} not found")
        if memory.superseded_by is not None:
            raise ValueError(
                f"memory {memory_id!r} is superseded — promote the "
                f"replacement ({memory.superseded_by!r}) instead"
            )

        # Heuristic scan for tenant-specific content. Findings are warnings,
        # not hard errors — the user has final say.
        warnings = _scan_for_tenant_specifics(memory, scope_hint=memory.org_alias)
        if warnings and not force:
            # Soft block — surface to the user. The CLI inspects `skipped`
            # and prompts "promote anyway?" before retrying with force=True.
            return PromotionResult(
                file=self.entries_dir / category / f"{_slugify(memory.name)}.md",
                warnings=warnings,
                skipped=True,
            )

        # Build the entry id + path.
        slug = _slugify(memory.name)
        entry_id = f"{_category_prefix(category)}-{slug}"
        target_dir = self.entries_dir / category
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{slug}.md"
        if path.exists():
            raise FileExistsError(
                f"refusing to overwrite existing knowledge entry {path}"
            )

        effective_title = title or memory.description or memory.name
        effective_tags = tags if tags is not None else list(memory.tags)
        effective_references = references or []

        path.write_text(
            _render_knowledge_entry(
                entry_id=entry_id,
                title=effective_title,
                category=category,
                severity=severity,
                tags=effective_tags,
                references=effective_references,
                body=memory.body,
                warnings=warnings,
            ),
            encoding="utf-8",
        )

        return PromotionResult(file=path, warnings=warnings, skipped=False)


# ---------------------------------------------------------------------------
# Tenant-specific-content heuristic
# ---------------------------------------------------------------------------

# Salesforce-style instance URLs and IDs that almost always signal
# tenant-specific data the user shouldn't be promoting cross-tenant.
_INSTANCE_URL_RE = re.compile(
    r"https?://[a-zA-Z0-9-]+\.(?:my\.salesforce\.com|lightning\.force\.com|"
    r"force\.com|salesforce\.com)",
    re.IGNORECASE,
)
_SF_ID_RE = re.compile(r"\b[a-zA-Z0-9]{15}(?:[a-zA-Z0-9]{3})?\b")
_ORG_ALIAS_TOKEN_RE = re.compile(r"\b(?:org|sandbox|prod)[A-Z][a-zA-Z]+\b")


def _scan_for_tenant_specifics(
    memory, scope_hint: str | None,
) -> list[str]:
    """Return human-readable warnings for content that looks tenant-specific.

    Heuristics, not certainties — false positives are expected. The user
    decides whether to override.
    """
    warnings: list[str] = []
    haystack = " ".join(filter(None, [
        memory.name, memory.description, memory.body,
        " ".join(memory.tags or []),
    ]))
    haystack_lower = haystack.lower()

    if scope_hint and scope_hint.lower() in haystack_lower:
        warnings.append(
            f"mentions org alias '{scope_hint}' — replace with a generic "
            "phrasing before promoting"
        )

    if _INSTANCE_URL_RE.search(haystack):
        warnings.append(
            "contains a Salesforce instance URL — strip before promoting"
        )

    sf_ids = _SF_ID_RE.findall(haystack)
    # 15/18-char tokens are very common in code identifiers too — only flag
    # if there's a sequence of them, suggesting an actual ID dump.
    if len(sf_ids) >= 2:
        warnings.append(
            f"looks like it contains {len(sf_ids)} Salesforce-shaped IDs "
            "— review before promoting"
        )

    org_token = _ORG_ALIAS_TOKEN_RE.search(haystack)
    if org_token:
        warnings.append(
            f"contains likely org alias token {org_token.group(0)!r} — "
            "rename to generic phrasing before promoting"
        )

    return warnings


# ---------------------------------------------------------------------------
# Knowledge-entry rendering
# ---------------------------------------------------------------------------

_CATEGORY_PREFIXES: dict[str, str] = {
    "governor_limit": "gl",
    "anti_pattern": "ap",
    "best_practice": "bp",
    "pattern": "pt",
}


def _category_prefix(category: str) -> str:
    return _CATEGORY_PREFIXES.get(category, "kb")


def _render_knowledge_entry(
    *,
    entry_id: str,
    title: str,
    category: str,
    severity: str | None,
    tags: list[str],
    references: list[str],
    body: str,
    warnings: list[str],
) -> str:
    lines = ["---"]
    lines.append(f"id: {_yaml_scalar(entry_id)}")
    lines.append(f"title: {_yaml_scalar(title)}")
    lines.append(f"category: {category}")
    lines.append(f"severity: {_yaml_optional(severity)}")
    lines.append(f"tags: {_yaml_list(tags)}")
    if references:
        lines.append("references:")
        for ref in references:
            lines.append(f"  - {_yaml_scalar(ref)}")
    else:
        lines.append("references: []")
    lines.append("---")
    lines.append("")
    if warnings:
        # Surface the heuristic warnings inline in the draft so the
        # reviewer can't miss them while editing.
        lines.append("<!--")
        lines.append("PROMOTION REVIEW: heuristics flagged this content as")
        lines.append("possibly tenant-specific. Edit the body to remove or")
        lines.append("generalize before merging:")
        for w in warnings:
            lines.append(f"  - {w}")
        lines.append("-->")
        lines.append("")
    lines.append(body.rstrip())
    lines.append("")
    return "\n".join(lines)
