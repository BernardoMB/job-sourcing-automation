"""The job pipeline: every opportunity, its stage, its threads. Mirrored to Notion when enabled."""

from __future__ import annotations

import re
from typing import Any

from steward.db import Database
from steward.mcp_hub import MCPHub
from steward.taxonomy import SIGNAL_TO_STAGE, advance_stage
from steward.timeutil import now_iso, parse_iso, to_iso


def _domain(address: str) -> str:
    return address.split("@")[-1].lower() if "@" in (address or "") else ""


def find_for_event(db: Database, event: dict[str, Any], cls: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if event.get("thread_key"):
        row = db.query_one(
            "SELECT o.* FROM opportunity_threads t JOIN opportunities o ON o.id = t.opportunity_id "
            "WHERE t.source = ? AND t.thread_key = ?", (event["source"], event["thread_key"]))
        if row:
            return row
    sender = (event.get("sender_address") or "").lower()
    if event.get("source") == "whatsapp":
        number = digits(sender)
        for o in db.query("SELECT * FROM opportunities WHERE ifnull(recruiter_phone, '') != '' "
                          "ORDER BY updated_at DESC") if number else []:
            if digits(o["recruiter_phone"]) == number:
                return o
    elif sender:
        row = db.query_one(
            "SELECT * FROM opportunities WHERE lower(recruiter_email) = ? OR lower(recruiter_linkedin) = ? "
            "ORDER BY updated_at DESC LIMIT 1", (sender, sender))
        if row:
            return row
    company = (cls or {}).get("company", "")
    if company:
        role = (cls or {}).get("role", "")
        rows = db.query("SELECT * FROM opportunities WHERE lower(company) = ? ORDER BY updated_at DESC",
                        (company.lower(),))
        for r in rows:
            if not role or not r.get("role") or r["role"].lower() == role.lower():
                return r
    return None


def digits(phone: str) -> str:
    d = re.sub(r"\D", "", phone or "")
    return d[-10:] if len(d) >= 7 else ""


def find_for_call(db: Database, cls: dict[str, Any]) -> dict[str, Any] | None:
    """Match a missed call / voicemail to an opportunity: by phone number, then company, then caller name.

    A call from a recruiter Job Sourcer has never seen becomes a new lead when the company is known.
    """
    number = digits(cls.get("phone_number", ""))
    if number:
        for o in db.query("SELECT * FROM opportunities WHERE recruiter_phone IS NOT NULL AND recruiter_phone != ''"):
            if digits(o["recruiter_phone"]) == number:
                return o
    if cls.get("company"):
        row = db.query_one("SELECT * FROM opportunities WHERE lower(company) = ? ORDER BY updated_at DESC",
                           (cls["company"].lower(),))
        if row:
            return row
    if cls.get("recruiter_name"):
        row = db.query_one("SELECT * FROM opportunities WHERE lower(recruiter_name) = ? ORDER BY updated_at DESC",
                           (cls["recruiter_name"].lower(),))
        if row:
            return row
    if not cls.get("company"):
        return None
    stamp = now_iso()
    opp_id = db.insert("opportunities", {
        "company": cls["company"], "role": cls.get("role", ""), "stage": "new",
        "recruiter_name": cls.get("recruiter_name", ""), "recruiter_phone": cls.get("phone_number", ""),
        "channel": "phone", "last_inbound_at": stamp, "created_at": stamp, "updated_at": stamp,
    })
    return db.get_opportunity(opp_id)


def upsert_for_event(db: Database, event: dict[str, Any], cls: dict[str, Any]) -> dict[str, Any]:
    opp = find_for_event(db, event, cls)
    stamp = now_iso()
    proposed_stage = SIGNAL_TO_STAGE.get(cls.get("stage_signal", "none"), "")
    sender = (event.get("sender_address") or "").lower()
    is_linkedin = event["source"] == "linkedin"
    is_chat = event["source"] in ("linkedin", "whatsapp")
    phone = event.get("raw", {}).get("phone", "") if event["source"] == "whatsapp" else ""
    if opp is None:
        opp_id = db.insert("opportunities", {
            "company": cls.get("company") or (_domain(sender).split(".")[0].title() if sender and not is_chat else ""),
            "role": cls.get("role", ""),
            "stage": proposed_stage or "new",
            "recruiter_name": cls.get("recruiter_name") or event.get("sender_name", ""),
            "recruiter_email": "" if is_chat else sender,
            "recruiter_linkedin": event.get("raw", {}).get("profile_url", "") if is_linkedin else "",
            "recruiter_phone": phone or cls.get("phone_number", ""),
            "channel": event["source"],
            "location": cls.get("location", ""),
            "last_inbound_at": event.get("received_at") or stamp,
            "created_at": stamp,
            "updated_at": stamp,
        })
    else:
        opp_id = opp["id"]
        updates: dict[str, Any] = {"updated_at": stamp, "last_inbound_at": event.get("received_at") or stamp}
        for field, value in (("company", cls.get("company")), ("role", cls.get("role")),
                             ("recruiter_name", cls.get("recruiter_name")), ("location", cls.get("location")),
                             ("recruiter_phone", phone or cls.get("phone_number"))):
            if value and not opp.get(field):
                updates[field] = value
        if proposed_stage:
            updates["stage"] = advance_stage(opp["stage"], proposed_stage)
        db.update("opportunities", opp_id, updates)
    if event.get("thread_key"):
        db.execute("INSERT OR IGNORE INTO opportunity_threads (source, thread_key, opportunity_id) VALUES (?, ?, ?)",
                   (event["source"], event["thread_key"], opp_id))
    return db.get_opportunity(opp_id)  # type: ignore[return-value]


def set_stage(db: Database, opp_id: int, stage: str, force: bool = False) -> None:
    opp = db.get_opportunity(opp_id)
    if not opp:
        return
    new = stage if force else advance_stage(opp["stage"], stage)
    db.update("opportunities", opp_id, {"stage": new, "updated_at": now_iso()})


def record_outbound(db: Database, opp_id: int | None, next_action: str = "") -> None:
    if not opp_id:
        return
    updates: dict[str, Any] = {"last_outbound_at": now_iso(), "updated_at": now_iso()}
    if next_action:
        updates["next_action"] = next_action
    opp = db.get_opportunity(opp_id)
    if opp and opp["stage"] == "new":
        updates["stage"] = "contacted"
    db.update("opportunities", opp_id, updates)


async def notion_pull(hub: MCPHub, db: Database, opp: dict[str, Any]) -> dict[str, Any]:
    """Read the row from Notion. A stage you changed in Notion wins over Job Sourcer's cache."""
    if not hub.enabled("notion"):
        return opp
    res = await hub.call("notion", "query_opportunities", {"steward_id": opp["id"]})
    rows = res.get("results", [])
    if not rows:
        return opp
    row = rows[0]
    updates: dict[str, Any] = {"notion_page_id": row["page_id"]}
    edited = parse_iso(row.get("last_edited_time"))
    synced = parse_iso(opp.get("notion_synced_at"))
    if row.get("stage") and row["stage"] != opp["stage"] and (synced is None or (edited and edited > synced)):
        updates["stage"] = row["stage"]
    if row.get("next_action") and row["next_action"] != (opp.get("next_action") or "") and edited and synced and edited > synced:
        updates["notes"] = ((opp.get("notes") or "") + f"\n[Notion] {row['next_action']}").strip()
    db.update("opportunities", opp["id"], updates)
    return db.get_opportunity(opp["id"]) or opp


async def notion_push(hub: MCPHub, db: Database, opp_id: int) -> None:
    if not hub.enabled("notion"):
        return
    opp = db.get_opportunity(opp_id)
    if not opp:
        return
    res = await hub.call("notion", "upsert_opportunity", {
        "steward_id": opp["id"], "company": opp.get("company") or "", "role": opp.get("role") or "",
        "stage": opp["stage"], "recruiter": opp.get("recruiter_name") or "",
        "recruiter_email": opp.get("recruiter_email") or "", "channel": opp.get("channel") or "",
        "last_contact": (opp.get("last_inbound_at") or opp.get("last_outbound_at") or "")[:10],
        "next_action": opp.get("next_action") or "", "page_id": opp.get("notion_page_id") or "",
    })
    # Our own write bumps Notion's last_edited_time; record the later of the two so it is not
    # mistaken for an edit you made in Notion.
    edited = parse_iso(res.get("last_edited_time"))
    synced = max(now_iso(), to_iso(edited)) if edited else now_iso()
    db.update("opportunities", opp_id, {"notion_page_id": res.get("page_id", opp.get("notion_page_id")),
                                        "notion_synced_at": synced})
