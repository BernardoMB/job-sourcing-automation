"""Notion MCP server: your job pipeline as a Notion database (free API).

SQLite stays the operational store; Notion is the human-friendly view that you
can also edit. The Decision Router reads the pipeline through this server, so if
you move an opportunity to "Offer" in Notion, autonomy for that thread is
revoked on the next message.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from steward.servers.common import READ_ONLY_REMOTE, WRITE, guarded, make_server, server_config
from steward.taxonomy import STAGES

cfg = server_config()
n_cfg = cfg.notion
API = "https://api.notion.com/v1"
VERSION = "2022-06-28"
mcp = make_server("steward-notion", "Read and update the job pipeline database in Notion.")

PROPERTIES_SCHEMA = {
    "Name": {"title": {}},
    "Company": {"rich_text": {}},
    "Role": {"rich_text": {}},
    "Stage": {"select": {"options": [{"name": label} for label in STAGES.values()]}},
    "Recruiter": {"rich_text": {}},
    "Recruiter Email": {"email": {}},
    "Channel": {"select": {"options": [{"name": "email"}, {"name": "linkedin"}, {"name": "calendar"}]}},
    "Last Contact": {"date": {}},
    "Next Action": {"rich_text": {}},
    "Steward ID": {"number": {}},
}


def _client(token_env: str = "") -> httpx.Client:
    token_env = token_env or n_cfg.token_env
    token = os.environ.get(token_env, "")
    if not token:
        raise RuntimeError(f"Set {token_env} in .env (a Notion internal integration token)")
    return httpx.Client(
        base_url=API,
        headers={"Authorization": f"Bearer {token}", "Notion-Version": VERSION, "Content-Type": "application/json"},
        timeout=30,
    )


def _check(resp: httpx.Response) -> dict[str, Any]:
    if resp.status_code >= 400:
        raise RuntimeError(f"Notion API {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def _text(value: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": {"content": (value or "")[:1900]}}] if value else []


def _plain(prop: dict[str, Any]) -> Any:
    kind = prop.get("type")
    val = prop.get(kind)
    if kind in ("title", "rich_text"):
        return "".join(t.get("plain_text", "") for t in val or [])
    if kind == "select":
        return (val or {}).get("name", "")
    if kind == "date":
        return (val or {}).get("start", "")
    if kind in ("email", "number", "url", "phone_number"):
        return val
    return val


def _norm(page: dict[str, Any]) -> dict[str, Any]:
    props = {k: _plain(v) for k, v in page.get("properties", {}).items()}
    label_to_stage = {v: k for k, v in STAGES.items()}
    return {
        "page_id": page["id"],
        "url": page.get("url", ""),
        "steward_id": int(props.get("Steward ID") or 0),
        "company": props.get("Company", ""),
        "role": props.get("Role", ""),
        "stage": label_to_stage.get(props.get("Stage", ""), ""),
        "stage_label": props.get("Stage", ""),
        "recruiter": props.get("Recruiter", ""),
        "recruiter_email": props.get("Recruiter Email") or "",
        "channel": props.get("Channel", ""),
        "last_contact": props.get("Last Contact", ""),
        "next_action": props.get("Next Action", ""),
        "last_edited_time": page.get("last_edited_time", ""),
    }


def _db_id() -> str:
    if not n_cfg.database_id:
        raise RuntimeError("Set notion.database_id in steward.yaml (or run `steward notion-init`)")
    return n_cfg.database_id


def _query(filter_: dict[str, Any] | None, limit: int = 100) -> list[dict[str, Any]]:
    body: dict[str, Any] = {"page_size": min(limit, 100)}
    if filter_:
        body["filter"] = filter_
    with _client() as c:
        data = _check(c.post(f"/databases/{_db_id()}/query", json=body))
    return [_norm(p) for p in data.get("results", [])]


@mcp.tool(annotations=READ_ONLY_REMOTE)
@guarded
def notion_status() -> dict[str, Any]:
    """Check that the token works and the pipeline database is reachable."""
    with _client() as c:
        data = _check(c.get(f"/databases/{_db_id()}"))
    title = "".join(t.get("plain_text", "") for t in data.get("title", []))
    return {"ok": True, "database_id": data["id"], "title": title, "url": data.get("url", "")}


@mcp.tool(annotations=WRITE)
@guarded
def create_database(parent_page_id: str, title: str = "Job Pipeline (Job Sourcer)") -> dict[str, Any]:
    """Create the pipeline database under a Notion page shared with the integration."""
    body = {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "title": _text(title),
        "properties": PROPERTIES_SCHEMA,
    }
    with _client() as c:
        data = _check(c.post("/databases", json=body))
    return {"database_id": data["id"], "url": data.get("url", "")}


@mcp.tool(annotations=WRITE)
@guarded
def archive_opportunity(page_id: str) -> dict[str, Any]:
    """Take a pipeline row out of the database (Notion archives it; it can be restored from Trash)."""
    with _client() as c:
        data = _check(c.patch(f"/pages/{page_id}", json={"archived": True}))
    return {"page_id": data["id"], "archived": bool(data.get("archived"))}


@mcp.tool(annotations=READ_ONLY_REMOTE)
@guarded
def query_opportunities(steward_id: int = 0, company: str = "", recruiter_email: str = "") -> dict[str, Any]:
    """Find pipeline rows by Steward ID, company, or recruiter email."""
    filters = []
    if steward_id:
        filters.append({"property": "Steward ID", "number": {"equals": steward_id}})
    if company:
        filters.append({"property": "Company", "rich_text": {"equals": company}})
    if recruiter_email:
        filters.append({"property": "Recruiter Email", "email": {"equals": recruiter_email}})
    filt = None
    if len(filters) == 1:
        filt = filters[0]
    elif filters:
        filt = {"or": filters}
    return {"results": _query(filt)}


@mcp.tool(annotations=READ_ONLY_REMOTE)
@guarded
def list_opportunities(limit: int = 100) -> dict[str, Any]:
    """All pipeline rows (most recent 100)."""
    return {"results": _query(None, limit)}


@mcp.tool(annotations=WRITE)
@guarded
def upsert_opportunity(
    steward_id: int,
    company: str,
    role: str,
    stage: str,
    recruiter: str = "",
    recruiter_email: str = "",
    channel: str = "",
    last_contact: str = "",
    next_action: str = "",
    page_id: str = "",
) -> dict[str, Any]:
    """Create or update the pipeline row for one opportunity."""
    props: dict[str, Any] = {
        "Name": {"title": _text(f"{company or 'Unknown company'} - {role or 'Role TBD'}")},
        "Company": {"rich_text": _text(company)},
        "Role": {"rich_text": _text(role)},
        "Stage": {"select": {"name": STAGES.get(stage, stage) or "New"}},
        "Recruiter": {"rich_text": _text(recruiter)},
        "Recruiter Email": {"email": recruiter_email or None},
        "Next Action": {"rich_text": _text(next_action)},
        "Steward ID": {"number": steward_id},
    }
    if channel:
        props["Channel"] = {"select": {"name": channel}}
    if last_contact:
        props["Last Contact"] = {"date": {"start": last_contact}}
    with _client() as c:
        if not page_id:
            existing = _query({"property": "Steward ID", "number": {"equals": steward_id}}, 1)
            page_id = existing[0]["page_id"] if existing else ""
        if page_id:
            data = _check(c.patch(f"/pages/{page_id}", json={"properties": props}))
        else:
            data = _check(c.post("/pages", json={"parent": {"database_id": _db_id()}, "properties": props}))
    return _norm(data)


def _pages(c: httpx.Client, path: str, body: dict[str, Any], limit: int = 1000) -> list[dict[str, Any]]:
    """Every result of a paginated Notion POST (search, database query)."""
    out: list[dict[str, Any]] = []
    cursor = None
    while len(out) < limit:
        data = _check(c.post(path, json={**body, "page_size": 100, **({"start_cursor": cursor} if cursor else {})}))
        out += data.get("results", [])
        cursor = data.get("next_cursor")
        if not data.get("has_more") or not cursor:
            break
    return out


def _moment(value: str, zone: str) -> datetime | None:
    """A Notion date-time as an aware datetime (Notion gives an offset, or a separate time zone)."""
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=ZoneInfo(zone))


def entries_from_page(page: dict[str, Any], database: str, workspace: str, zone: str,
                      default_minutes: int) -> tuple[list[dict[str, Any]], int]:
    """The calendar entries of one database row (one per date property with a time), and how many dates had no time."""
    props = page.get("properties", {})
    title = next(("".join(t.get("plain_text", "") for t in v.get("title") or [])
                  for v in props.values() if v.get("type") == "title"), "") or "Untitled"
    out, all_day = [], 0
    for name, prop in props.items():
        val = prop.get("date") if prop.get("type") == "date" else None
        if not val or not val.get("start"):
            continue
        if "T" not in val["start"]:
            all_day += 1  # a date without a time: a deadline or an all-day note, not a meeting
            continue
        tz_name = val.get("time_zone") or zone
        start = _moment(val["start"], tz_name)
        end = _moment(val["end"], tz_name) if val.get("end") and "T" in val["end"] else None
        if start is None:
            continue
        if end is None or end <= start:
            end = start + timedelta(minutes=default_minutes)
        out.append({"uid": f"notion-{page['id']}-{prop.get('id') or name}@jobsourcer", "title": title,
                    "start": start.isoformat(), "end": end.isoformat(), "url": page.get("url", ""),
                    "database": database, "workspace": workspace, "property": name})
    return out, all_day


def _workspace(c: httpx.Client) -> str:
    me = _check(c.get("/users/me"))
    return (me.get("bot") or {}).get("workspace_name") or me.get("name") or ""


def _databases(c: httpx.Client) -> list[dict[str, Any]]:
    return [d for d in _pages(c, "/search", {"filter": {"property": "object", "value": "database"}}, 500)
            if not d.get("archived") and not d.get("in_trash")]


@mcp.tool(annotations=READ_ONLY_REMOTE)
@guarded
def workspace_status(token_env: str = "") -> dict[str, Any]:
    """Which workspace a token opens, and the databases shared with its integration (with their date columns)."""
    with _client(token_env) as c:
        workspace = _workspace(c)
        dbs = _databases(c)
    return {"workspace": workspace, "databases": [
        {"id": d["id"], "title": "".join(t.get("plain_text", "") for t in d.get("title", [])) or "Untitled database",
         "date_properties": [n for n, p in d.get("properties", {}).items() if p.get("type") == "date"]}
        for d in dbs]}


@mcp.tool(annotations=READ_ONLY_REMOTE)
@guarded
def scheduled_entries(days: int = 60, token_env: str = "", exclude_database_id: str = "",
                      default_minutes: int = 60) -> dict[str, Any]:
    """Rows with an upcoming date AND time, in every database this workspace's integration can see.

    One entry per date property. Dates without a time are counted, not returned."""
    zone = cfg.profile.timezone
    today = datetime.now(ZoneInfo(zone)).date()
    until = today + timedelta(days=days)
    skip = (exclude_database_id or "").replace("-", "")
    entries: list[dict[str, Any]] = []
    all_day = 0
    with _client(token_env) as c:
        workspace = _workspace(c)
        databases = _databases(c)
        for db in databases:
            if db["id"].replace("-", "") == skip:
                continue
            dates = [n for n, p in db.get("properties", {}).items() if p.get("type") == "date"]
            if not dates:
                continue
            name = "".join(t.get("plain_text", "") for t in db.get("title", [])) or "Untitled database"
            window = [{"and": [{"property": n, "date": {"on_or_after": today.isoformat()}},
                               {"property": n, "date": {"on_or_before": until.isoformat()}}]} for n in dates]
            filt = {"or": window} if len(window) > 1 else window[0]
            for page in _pages(c, f"/databases/{db['id']}/query", {"filter": filt}):
                found, undated = entries_from_page(page, name, workspace, zone, default_minutes)
                all_day += undated
                entries += [e for e in found if today.isoformat() <= e["start"][:10] <= until.isoformat()]
    return {"workspace": workspace, "databases": len(databases), "entries": entries, "date_only": all_day}


if __name__ == "__main__":
    mcp.run()
