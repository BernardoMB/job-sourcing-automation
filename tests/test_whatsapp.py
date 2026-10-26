"""WhatsApp: chats about a job are handled like LinkedIn conversations; every other chat is ignored completely."""

from __future__ import annotations

import asyncio
import importlib
import json
import sqlite3

import pytest

from steward import briefing, decisions
from steward.timeutil import now_iso
from steward.workspace import inject_whatsapp

from .conftest import run, sandbox_json

RECRUITER = "5215512345678@s.whatsapp.net"
PERSONAL = ("Mamá", "Diego", "Familia", "mole", "partido", "primo")


REPLY = "¡Hola Mariana! Me interesa mucho. ¿Te parece el jueves a las 10?"


def _whatsapp_draft(steward) -> dict:
    return steward.db.query_one("SELECT * FROM drafts WHERE channel = 'whatsapp' ORDER BY id DESC")


def _approve_with_reply(steward, draft: dict) -> None:
    """Approve with your own wording (the offline drafter leaves the Spanish question for you to answer)."""
    edits = [{"type": a["type"], "payload": {"text": REPLY}} if a["type"] == "send_whatsapp" else {"type": a["type"]}
             for a in json.loads(draft["actions_json"])]
    decisions.stage(steward.db, draft["id"], "approve", edits)


def test_only_job_chats_are_kept_and_answered(steward, chimes):
    summary = run(steward.cycle(force=True))
    assert summary["errors"] == []
    assert len(chimes) == 1  # one chime for the whole check

    events = steward.db.query("SELECT * FROM events WHERE source = 'whatsapp'")
    kept = [e for e in events if e["status"] != "ignored"]
    assert [e["thread_key"] for e in kept] == [RECRUITER]
    # The family chat (no job words) and the group were never stored. The friend's chat mentions "trabajo",
    # so it was classified, found personal, and nothing of it was kept.
    ignored = [e for e in events if e["status"] == "ignored"]
    assert len(ignored) == 1 and ignored[0]["body_text"] == "" and ignored[0]["sender_name"] == ""
    assert ignored[0]["raw_json"] == "{}" and ignored[0]["classification_json"] is None
    stored = json.dumps(steward.db.query("SELECT * FROM events") + steward.db.query("SELECT * FROM activity"),
                        ensure_ascii=False)
    assert not any(word in stored for word in PERSONAL)
    assert "WhatsApp message from Mariana López" in stored and "WhatsApp: 1 new" in stored
    assert briefing.build(steward.cfg, steward.db)["counts"]["ignored"] == \
        steward.db.scalar("SELECT COUNT(*) FROM events WHERE status = 'ignored' AND source != 'whatsapp'")

    draft = _whatsapp_draft(steward)
    actions = json.loads(draft["actions_json"])
    send = next(a for a in actions if a["type"] == "send_whatsapp")
    assert send["payload"]["conversation_id"] == RECRUITER and send["payload"]["recipient"] == "Mariana López"
    opp = steward.db.get_opportunity(draft["opportunity_id"])
    assert opp["recruiter_phone"] == "+5215512345678" and opp["channel"] == "whatsapp"
    assert opp["recruiter_email"] == ""

    _approve_with_reply(steward, draft)
    result = run(steward.go())
    assert result["executed"] >= 1 and result["failed"] == 0
    sent = sandbox_json(steward, "whatsapp.json")["sent"]
    assert [m["chat_id"] for m in sent] == [RECRUITER] and sent[0]["text"] == REPLY
    out = steward.db.query_one("SELECT * FROM outbound WHERE draft_id = ?", (draft["id"],))
    assert out["channel"] == "whatsapp" and out["thread_key"] == RECRUITER

    # Her next message is a known chat: picked up even without job words.
    inject_whatsapp(steward.cfg, "Mariana López", "Perfecto, gracias!", RECRUITER)
    run(steward.cycle(force=True))
    assert steward.db.scalar("SELECT COUNT(*) FROM events WHERE source = 'whatsapp' AND thread_key = ?",
                             (RECRUITER,)) == 2


def test_replying_yourself_in_whatsapp_closes_the_item(steward):
    run(steward.cycle(force=True))
    draft = _whatsapp_draft(steward)
    store = steward.cfg.sandbox_path / "whatsapp.json"
    data = json.loads(store.read_text(encoding="utf-8"))
    chat = next(c for c in data["chats"] if c["id"] == RECRUITER)
    chat["messages"].append({"id": "wa-mine", "from": "me", "sent_at": now_iso(), "text": "¡Claro! Hablamos el jueves."})
    store.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    run(steward.cycle(force=True))
    closed = steward.db.get_draft(draft["id"])
    assert closed["status"] == "superseded" and "You already replied on WhatsApp" in closed["status_reason"]
    assert sandbox_json(steward, "whatsapp.json")["sent"] == []


def test_whatsapp_replies_are_paced(steward):
    run(steward.cycle(force=True))
    steward.cfg.whatsapp.business_hours = "00:00-00:00"  # never open: the send waits
    path = steward.cfg.config_path
    import yaml

    data = yaml.safe_load(open(path, encoding="utf-8"))
    data["whatsapp"]["business_hours"] = "00:00-00:00"
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False)
    draft = _whatsapp_draft(steward)
    _approve_with_reply(steward, draft)
    run(steward.go())
    assert steward.db.get_draft(draft["id"])["status"] == "deferred"
    assert "WhatsApp pacing" in steward.db.get_draft(draft["id"])["status_reason"]
    assert sandbox_json(steward, "whatsapp.json")["sent"] == []


@pytest.fixture()
def wa_server(workspace, monkeypatch):
    monkeypatch.setenv("STEWARD_CONFIG", str(workspace))
    import steward.servers.whatsapp as module

    return importlib.reload(module)


def test_never_writes_in_groups(wa_server):
    with pytest.raises(Exception, match="groups"):
        asyncio.run(wa_server.send_message("120363000000000001@g.us", "hola"))


def test_upstream_contract(wa_server, monkeypatch):
    """whatsapp-mcp-ts: chats with jid/name/last_message_time, messages with content/timestamp/is_from_me."""
    from steward.config import ServerSpec

    monkeypatch.setattr(wa_server.wa_cfg, "upstream", ServerSpec(command="node", args=["C:/wa/src/main.ts"]))
    up = wa_server.UpstreamWhatsApp()
    recent = now_iso()
    chats = [
        {"jid": RECRUITER, "name": "Mariana López", "is_group": False, "last_message_time": recent,
         "last_is_from_me": False},
        {"jid": "120363000000000001@g.us", "name": "Familia", "is_group": True, "last_message_time": recent},
        {"jid": "5215500000000@s.whatsapp.net", "name": "Old", "last_message_time": "2020-01-01T00:00:00Z"},
        {"jid": "5215511111111@s.whatsapp.net", "name": "Me last", "last_message_time": recent,
         "last_is_from_me": True},
    ]
    messages = [  # newest first, as the upstream returns them
        {"id": "B", "content": "¿Te interesa la vacante?", "timestamp": recent, "is_from_me": False},
        {"id": "A", "content": "Hola", "timestamp": "2026-01-01T10:00:00.000Z", "is_from_me": True},
    ]
    calls = []

    async def fake_call(logical, args):
        calls.append((logical, args))
        if logical == "list_chats":
            return chats
        if logical == "list_messages":
            return messages if args["chat_jid"] == RECRUITER else "No messages found."
        return f"Message sent successfully to {args['recipient']} (ID: 3EB0ABC)."

    monkeypatch.setattr(up, "call", fake_call)
    convs = asyncio.run(up.list(None, 10))
    assert [c["id"] for c in convs] == [RECRUITER]
    assert convs[0]["phone"] == "+5215512345678" and convs[0]["participant_name"] == "Mariana López"
    assert [(m["id"], m["from"]) for m in convs[0]["messages"]] == [("A", "me"), ("B", "them")]
    assert [c[1]["chat_jid"] for c in calls if c[0] == "list_messages"] == [RECRUITER]
    assert asyncio.run(up.send(RECRUITER, "Hola Mariana"))["message_id"] == "3EB0ABC"
    assert asyncio.run(up.get(RECRUITER, 5))["participant_name"] == "Mariana López"  # name remembered


def test_unlinked_upstream_is_never_started(wa_server, monkeypatch, tmp_path):
    from steward.config import ServerSpec

    main = tmp_path / "whatsapp-mcp-ts" / "src" / "main.ts"
    monkeypatch.setattr(wa_server.wa_cfg, "upstream", ServerSpec(command="node", args=[str(main)]))
    with pytest.raises(RuntimeError, match="not linked"):
        asyncio.run(wa_server.UpstreamWhatsApp().call("list_chats", {}))


def test_link_helpers_keep_the_qr_code_local(workspace, tmp_path):
    from steward import setup as setup_mod
    from steward import whatsapp_link as wl
    from steward.config import load_config

    folder = tmp_path / "whatsapp-mcp-ts"
    (folder / "src").mkdir(parents=True)
    (folder / "node_modules").mkdir()
    (folder / "src" / "main.ts").write_text("// main", encoding="utf-8")
    (folder / "src" / "whatsapp.ts").write_text(f"if (qr) {{\n  {wl._QR_LINE}\n}}\n", encoding="utf-8")
    (folder / "src" / "database.ts").write_bytes(  # CRLF, as git checks it out on Windows
        b"last_message_time:\r\n  x instanceof Date\r\n    ? x\r\n" + wl._TIME_LINE.replace("\n", "\r\n").encode()
        + b"            ? null\r\n")
    (folder / "data").mkdir()
    db = sqlite3.connect(folder / "data" / "whatsapp.db")  # the upstream's schema, with its "undefined" times
    db.executescript("CREATE TABLE chats (jid TEXT PRIMARY KEY, name TEXT, last_message_time TEXT);"
                     "CREATE TABLE messages (id TEXT, chat_jid TEXT, timestamp TEXT);"
                     "INSERT INTO chats VALUES ('a@lid', 'A', 'undefined'), ('b@lid', 'B', 'undefined'),"
                     " ('c@s.whatsapp.net', 'C', '2026-01-01T00:00:00.000Z');"
                     "INSERT INTO messages VALUES ('1', 'a@lid', '2026-10-26T10:00:00.000Z'),"
                     " ('2', 'c@s.whatsapp.net', '2026-10-25T10:00:00.000Z');")
    db.close()
    assert wl.installed(folder) and not wl.linked(folder) and wl.patched(folder) is False
    wl.patch(folder)
    wl.patch(folder)  # idempotent
    assert wl.patched(folder) and "quickchart" not in (folder / "src" / "whatsapp.ts").read_text(encoding="utf-8")
    assert "== null // Job Sourcer patch" in (folder / "src" / "database.ts").read_text(encoding="utf-8")
    db = sqlite3.connect(folder / "data" / "whatsapp.db")
    times = dict(db.execute("SELECT jid, last_message_time FROM chats"))
    db.close()
    assert times == {"a@lid": "2026-10-26T10:00:00.000Z", "b@lid": None, "c@s.whatsapp.net": "2026-10-25T10:00:00.000Z"}

    cfg = wl.configure(load_config(workspace), folder)
    assert cfg.whatsapp.backend == "upstream" and wl.folder_of(cfg) == folder.resolve()
    step = next(c for c in setup_mod.checks(cfg) if c["id"] == "whatsapp")
    assert step["status"] == "todo" and "not linked" in step["detail"]
    (folder / "auth_info").mkdir()
    (folder / "auth_info" / "creds.json").write_text("{}", encoding="utf-8")
    assert next(c for c in setup_mod.checks(cfg) if c["id"] == "whatsapp")["status"] == "ok"

    log = folder / "wa-logs.txt"
    log.write_text(json.dumps({"qrCodeData": "2@abc"}) + "\n" + json.dumps({"msg": "Connection opened. WA user: A"})
                   + "\n{\"msg\": \"partial", encoding="utf-8")
    entries = wl._new_entries(log, 0)
    assert [e.get("qrCodeData") for e in entries] == ["2@abc", None] and "opened" in entries[1]["msg"]
    assert wl._new_entries(log, entries[-1]["_offset"]) == []  # the half-written line waits
