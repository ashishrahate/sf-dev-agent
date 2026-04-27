"""ConversationLog — list-like wrapper that mirrors writes to working memory.

The agent loop uses `self.conversation: list[dict]` as a plain Python list.
Slice 2a needs every append to also land in SQLite so a crashed process can
resume. ConversationLog preserves the existing list-shaped contract (iter,
len, indexing, append) but writes through to a `WorkingMemoryStore` on
every append, so `agent.py`'s blast radius stays tiny.

Design notes:
    - **In-memory mirror.** We keep the messages in a Python list AND in
      SQLite. Hot reads (the LLM chat call iterates the conversation every
      iteration) hit RAM, not the DB. Writes go to both.
    - **Append-only.** The agent never edits or removes messages mid-run, so
      we don't need to expose `__setitem__`/`pop`/`remove`. If that ever
      changes, the DB-side reconciliation needs new code.
    - **Optional store.** Pass `store=None` for tests / one-off runs that
      don't want persistence; the wrapper degrades to a plain list.
    - **Resume.** Construct with `seed=` to pre-fill the in-memory list from
      a previously-loaded transcript. The store is NOT re-written for seeded
      messages — they're already there.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from sf_dev_agent.memory.working import WorkingMemoryStore


class ConversationLog:
    """List-shaped conversation with optional write-through to SQLite."""

    def __init__(
        self,
        task_id: str,
        store: WorkingMemoryStore | None = None,
        seed: list[dict[str, Any]] | None = None,
    ) -> None:
        self._task_id = task_id
        self._store = store
        self._messages: list[dict[str, Any]] = list(seed or [])

    # ------------------------------------------------------------------
    # List-shaped surface
    # ------------------------------------------------------------------

    def append(self, message: dict[str, Any]) -> None:
        """Append + persist. Persistence failure does NOT swallow data —
        the in-memory list is always updated first so the agent's run
        continues even if the DB is briefly unavailable.
        """
        self._messages.append(message)
        if self._store is not None:
            try:
                self._store.append_message(
                    self._task_id,
                    role=message["role"],
                    content=message["content"],
                )
            except Exception:
                # Persistence is best-effort. The agent must keep running
                # rather than crash on a transient SQLite write — but the
                # error needs to surface somewhere.
                import logging
                logging.getLogger(__name__).exception(
                    "Failed to persist conversation message for task %s",
                    self._task_id,
                )

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(self._messages)

    def __len__(self) -> int:
        return len(self._messages)

    def __getitem__(self, index: int | slice) -> Any:
        return self._messages[index]

    def __bool__(self) -> bool:
        return bool(self._messages)

    # ------------------------------------------------------------------
    # Provider-friendly view
    # ------------------------------------------------------------------

    def as_messages(self) -> list[dict[str, Any]]:
        """Return the underlying list — providers expect a real list."""
        return self._messages

    @property
    def task_id(self) -> str:
        return self._task_id
