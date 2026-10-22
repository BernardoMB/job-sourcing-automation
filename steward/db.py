"""SQLite operational store. All operational state lives here; Notion is a mirror."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from steward.timeutil import now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,                 -- email | linkedin | calendar | steward
    external_id TEXT NOT NULL,
    thread_key TEXT,
    sender_name TEXT,
    sender_address TEXT,
    subject TEXT,
    body_text TEXT,
    received_at TEXT,
    raw_json TEXT,
    status TEXT NOT NULL DEFAULT 'new',   -- new | processed | info | ignored | error
    classification_json TEXT,
    opportunity_id INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL,
    processed_at TEXT,
    UNIQUE (source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_events_status ON events(status);
CREATE INDEX IF NOT EXISTS idx_events_thread ON events(thread_key);

CREATE TABLE IF NOT EXISTS message_ids (
    message_id TEXT PRIMARY KEY,
    thread_key TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY,
    event_id INTEGER REFERENCES events(id),
    opportunity_id INTEGER,
    channel TEXT,
    summary TEXT,
    actions_json TEXT NOT NULL,
    final_actions_json TEXT,
    analysis_json TEXT,                   -- classification, CV evidence, scheduling, critique
    confidence REAL,
    job_advancement REAL,
    routing_band TEXT,                    -- HIGH | MID | LOW
    risk_level TEXT,
    priority TEXT NOT NULL DEFAULT 'normal',  -- high | normal
    route TEXT NOT NULL DEFAULT 'ask',    -- ask | auto_approve | auto_send
    route_reasons_json TEXT,
    matched_rule_id INTEGER,
    shadow_rule_ids_json TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    decision_note TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    executed_at TEXT,
    acknowledged_at TEXT,
    status_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_drafts_status ON drafts(status);

CREATE TABLE IF NOT EXISTS executions (
    id INTEGER PRIMARY KEY,
    draft_id INTEGER NOT NULL,
    action_index INTEGER NOT NULL,
    action_type TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,                 -- in_flight | success | failed | deferred | handed_off | unknown
    result_json TEXT,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 1,
    next_attempt_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outbound (
    id INTEGER PRIMARY KEY,
    draft_id INTEGER,
    channel TEXT NOT NULL,
    thread_key TEXT,
    recipient TEXT,
    subject TEXT,
    body TEXT,
    external_id TEXT,
    sent_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outbound_thread ON outbound(thread_key);

CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    condition_json TEXT NOT NULL,
    tier TEXT NOT NULL,                   -- auto_approve | auto_send
    min_confidence REAL NOT NULL,
    status TEXT NOT NULL,                 -- proposed | shadow | active | dormant | retired
    origin TEXT NOT NULL,                 -- user | learner
    created_from_draft_id INTEGER,
    evidence_json TEXT,
    shadow_agreements INTEGER NOT NULL DEFAULT 0,
    shadow_disagreements INTEGER NOT NULL DEFAULT 0,
    times_matched INTEGER NOT NULL DEFAULT 0,
    times_executed INTEGER NOT NULL DEFAULT 0,
    overrides INTEGER NOT NULL DEFAULT 0,
    last_matched_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS preferences (
    id INTEGER PRIMARY KEY,
    text TEXT NOT NULL,
    categories_json TEXT,
    status TEXT NOT NULL,                 -- proposed | active | retired
    origin TEXT NOT NULL,                 -- user | learner
    evidence_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Things only you can do: take an assessment, fill a form, prepare a document, apply via a link.
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    details TEXT,
    kind TEXT NOT NULL DEFAULT 'other',   -- assessment | form | document | application | prepare | other
    link TEXT,
    due_at TEXT,
    status TEXT NOT NULL DEFAULT 'open',  -- suggested | open | done | dismissed
    origin TEXT NOT NULL,                 -- steward | you
    opportunity_id INTEGER,
    event_id INTEGER,
    draft_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);

CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,                   -- new_rule | promote_rule | review_rule | new_preference | rule_graduated | rule_dormant
    signature TEXT,
    target_id INTEGER,
    title TEXT NOT NULL,
    rationale TEXT,
    payload_json TEXT,
    status TEXT NOT NULL DEFAULT 'open',  -- open | accepted | dismissed | info
    created_at TEXT NOT NULL,
    decided_at TEXT
);

CREATE TABLE IF NOT EXISTS opportunities (
    id INTEGER PRIMARY KEY,
    company TEXT,
    role TEXT,
    stage TEXT NOT NULL DEFAULT 'new',
    recruiter_name TEXT,
    recruiter_email TEXT,
    recruiter_linkedin TEXT,
    channel TEXT,
    location TEXT,
    notes TEXT,
    next_action TEXT,
    last_inbound_at TEXT,
    last_outbound_at TEXT,
    notion_page_id TEXT,
    notion_synced_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS opportunity_threads (
    source TEXT NOT NULL,
    thread_key TEXT NOT NULL,
    opportunity_id INTEGER NOT NULL,
    PRIMARY KEY (source, thread_key)
);

CREATE TABLE IF NOT EXISTS subagent_calls (
    id INTEGER PRIMARY KEY,
    subagent TEXT NOT NULL,
    event_id INTEGER,
    model TEXT,
    input_json TEXT,
    output_json TEXT,
    latency_ms INTEGER,
    error TEXT,
    created_at TEXT NOT NULL
);

-- What the app did, for the Logs tab: every check, new message, action, and what blocked it.
CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,                   -- check | message | draft | action | calendar | system
    level TEXT NOT NULL DEFAULT 'info',   -- info | warn | error
    message TEXT NOT NULL,
    details_json TEXT,
    draft_id INTEGER,
    event_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_activity_at ON activity(at);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT,
    channels_json TEXT,
    created_at TEXT NOT NULL
);

-- The decision ledger: append-only and hash-chained. Never updated, never deleted.
CREATE TABLE IF NOT EXISTS ledger (
    seq INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    event_id INTEGER,
    draft_id INTEGER,
    rule_id INTEGER,
    payload_json TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_kind ON ledger(kind);
CREATE TRIGGER IF NOT EXISTS ledger_no_update BEFORE UPDATE ON ledger
BEGIN SELECT RAISE(ABORT, 'the decision ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS ledger_no_delete BEFORE DELETE ON ledger
BEGIN SELECT RAISE(ABORT, 'the decision ledger is append-only'); END;
"""


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def loads(value: str | None, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


# Columns added after the first release: (table, column, type). Applied on open.
MIGRATIONS = [
    ("drafts", "priority_score", "REAL"),
    ("drafts", "priority_why_json", "TEXT"),
    ("opportunities", "recruiter_phone", "TEXT"),
    ("preferences", "company", "TEXT"),
    ("preferences", "sender", "TEXT"),
    ("drafts", "revised_from", "INTEGER"),
    ("drafts", "closed_at", "TEXT"),      # when it was discarded, or found no longer needed
]


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=30, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.executescript(SCHEMA)
        for table, column, kind in MIGRATIONS:
            cols = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")

    def close(self) -> None:
        self.conn.close()

    # -- basic helpers -------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, tuple(params))

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(sql, tuple(params)).fetchall()]

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        row = self.conn.execute(sql, tuple(params)).fetchone()
        return dict(row) if row else None

    def scalar(self, sql: str, params: Iterable[Any] = ()) -> Any:
        row = self.conn.execute(sql, tuple(params)).fetchone()
        return row[0] if row else None

    def insert(self, table: str, values: dict[str, Any]) -> int:
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        cur = self.conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(values.values()))
        return int(cur.lastrowid)

    def update(self, table: str, row_id: int, values: dict[str, Any], key: str = "id") -> None:
        if not values:
            return
        sets = ", ".join(f"{k} = ?" for k in values)
        self.conn.execute(f"UPDATE {table} SET {sets} WHERE {key} = ?", (*values.values(), row_id))

    # -- key/value state ----------------------------------------------
    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self.query_one("SELECT value FROM kv WHERE key = ?", (key,))
        return loads(row["value"], default) if row else default

    def kv_set(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, dumps(value)),
        )

    # -- domain helpers -----------------------------------------------
    def get_event(self, event_id: int) -> dict[str, Any] | None:
        return self.query_one("SELECT * FROM events WHERE id = ?", (event_id,))

    def get_draft(self, draft_id: int) -> dict[str, Any] | None:
        return self.query_one("SELECT * FROM drafts WHERE id = ?", (draft_id,))

    def get_rule(self, rule_id: int) -> dict[str, Any] | None:
        return self.query_one("SELECT * FROM rules WHERE id = ?", (rule_id,))

    def get_opportunity(self, opp_id: int | None) -> dict[str, Any] | None:
        if not opp_id:
            return None
        return self.query_one("SELECT * FROM opportunities WHERE id = ?", (opp_id,))

    def log_subagent(
        self,
        subagent: str,
        event_id: int | None,
        input_obj: Any,
        output_obj: Any,
        latency_ms: int,
        model: str = "",
        error: str = "",
    ) -> None:
        self.insert(
            "subagent_calls",
            {
                "subagent": subagent,
                "event_id": event_id,
                "model": model,
                "input_json": dumps(input_obj),
                "output_json": dumps(output_obj),
                "latency_ms": latency_ms,
                "error": error or None,
                "created_at": now_iso(),
            },
        )
