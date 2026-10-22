"""Notion MCP server: your job pipeline as a Notion database (free API).

SQLite stays the operational store; Notion is the human-friendly view that you
can also edit. The Decision Router reads the pipeline through this server, so if
you move an opportunity to "Offer" in Notion, autonomy for that thread is
revoked on the next message.
"""

from __future__ import annotations

import os
from typing import Any

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


def _client() -> httpx.Client:
    token = os.environ.get(n_cfg.token_env, "")
    if not token:
        raise RuntimeError(f"Set {n_cfg.token_env} in .env (a Notion internal integration token)")
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


if __name__ == "__main__":
    mcp.run()
