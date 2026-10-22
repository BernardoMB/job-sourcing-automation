"""The decision ledger: append-only, hash-chained, tamper-evident.

Every decision you make, every action taken on your behalf, every rule change,
and every override is appended here with full context. The learning engine only
reads from it. `verify()` recomputes the chain, so any edit or deletion made
outside Job Sourcer (the table also has triggers that block UPDATE and DELETE) is
detected.
"""

from __future__ import annotations

import hashlib
from typing import Any

from steward.db import Database, dumps, loads
from steward.timeutil import now_iso

GENESIS = "0" * 64

KINDS = {
    "decision",          # you approved / edited / rejected a draft
    "auto_execution",    # Job Sourcer acted on its own under an earned rule
    "execution",         # outcome of one executed action
    "override",          # you flagged an automatic action as wrong
    "rule_change",       # a rule was created, promoted, demoted, retired, graduated
    "preference_change", # a preference was created or retired
    "proposal",          # you accepted or dismissed a suggested automation
    "task",              # a task was suggested, accepted, added, completed, or dismissed
    "instruction",       # you told Job Sourcer what to do instead of its draft
}


def _digest(prev_hash: str, seq: int, ts: str, kind: str, event_id, draft_id, rule_id, payload_json: str) -> str:
    material = "|".join(
        [prev_hash, str(seq), ts, kind, str(event_id or ""), str(draft_id or ""), str(rule_id or ""), payload_json]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class Ledger:
    def __init__(self, db: Database):
        self.db = db

    def append(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        event_id: int | None = None,
        draft_id: int | None = None,
        rule_id: int | None = None,
    ) -> int:
        if kind not in KINDS:
            raise ValueError(f"unknown ledger kind: {kind}")
        payload_json = dumps(payload)
        conn = self.db.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            last = conn.execute("SELECT seq, hash FROM ledger ORDER BY seq DESC LIMIT 1").fetchone()
            seq = (last["seq"] + 1) if last else 1
            prev_hash = last["hash"] if last else GENESIS
            ts = now_iso()
            digest = _digest(prev_hash, seq, ts, kind, event_id, draft_id, rule_id, payload_json)
            conn.execute(
                "INSERT INTO ledger (seq, ts, kind, event_id, draft_id, rule_id, payload_json, prev_hash, hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (seq, ts, kind, event_id, draft_id, rule_id, payload_json, prev_hash, digest),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return seq

    def verify(self) -> dict[str, Any]:
        prev = GENESIS
        count = 0
        for row in self.db.conn.execute("SELECT * FROM ledger ORDER BY seq ASC"):
            count += 1
            if row["prev_hash"] != prev:
                return {"ok": False, "entries": count, "broken_at": row["seq"], "reason": "chain link mismatch"}
            expected = _digest(
                row["prev_hash"],
                row["seq"],
                row["ts"],
                row["kind"],
                row["event_id"],
                row["draft_id"],
                row["rule_id"],
                row["payload_json"],
            )
            if expected != row["hash"]:
                return {"ok": False, "entries": count, "broken_at": row["seq"], "reason": "content hash mismatch"}
            prev = row["hash"]
        return {"ok": True, "entries": count, "head": prev}

    def entries(self, kind: str | None = None, limit: int = 200, newest_first: bool = True) -> list[dict[str, Any]]:
        order = "DESC" if newest_first else "ASC"
        if kind:
            rows = self.db.query(f"SELECT * FROM ledger WHERE kind = ? ORDER BY seq {order} LIMIT ?", (kind, limit))
        else:
            rows = self.db.query(f"SELECT * FROM ledger ORDER BY seq {order} LIMIT ?", (limit,))
        for r in rows:
            r["payload"] = loads(r.pop("payload_json"), {})
        return rows

    def decisions(self) -> list[dict[str, Any]]:
        """All human decisions in chronological order (the training data)."""
        return self.entries("decision", limit=100000, newest_first=False)
