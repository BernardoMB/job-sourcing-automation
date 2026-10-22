"""The activity log behind the Logs tab.

Every check, every new message (and why one was ignored), every action taken or
deferred, and every error lands here as one plain sentence. Unlike the decision
ledger it is not evidence, just a readable trail, so old lines are pruned.
"""

from __future__ import annotations

from typing import Any

from steward.db import Database, dumps, loads
from steward.timeutil import now_iso

KEEP = 20000  # about two weeks with email checked every minute


def record(db: Database, kind: str, message: str, level: str = "info", *, details: dict[str, Any] | None = None,
           draft_id: int | None = None, event_id: int | None = None) -> None:
    row_id = db.insert("activity", {"at": now_iso(), "kind": kind, "level": level, "message": message,
                                    "details_json": dumps(details) if details else None,
                                    "draft_id": draft_id, "event_id": event_id})
    if row_id % 250 == 0:
        db.execute("DELETE FROM activity WHERE id <= ?", (row_id - KEEP,))


def recent(db: Database, limit: int = 2000) -> list[dict[str, Any]]:
    rows = db.query("SELECT * FROM activity ORDER BY id DESC LIMIT ?", (limit,))
    return [{**r, "details": loads(r.pop("details_json"), None)} for r in rows]
