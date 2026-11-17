"""What Job Sourcer reads inside the signed-in Notion page: workspaces, databases, dated items.

`browser` is notion_sync.PlaywrightMCP: each request runs inside the signed-in page through the Playwright
MCP server's browser_evaluate. It asks the Notion web app's own endpoints (app.notion.com/api/v3) for the
same records the app loads to draw your sidebar and databases, so it sees exactly what you see:

1. getSpaces: every workspace of every account signed in to the browser.
2. Per workspace, the pages in your sidebar (private, shared, teamspaces) and every page below
   them (loadCachedPageChunkV2), collecting the databases on those pages.
3. Per database, its rows (queryCollection) and the date property that holds each row's date.
"""

from __future__ import annotations

import re
from collections import deque
from datetime import date, timedelta
from typing import Any

MAX_PAGES_PER_WORKSPACE = 400
MAX_CHUNKS_PER_PAGE = 10
ROW_LIMIT = 1000
PREFERRED_DATE_NAMES = re.compile(r"date|time|when|start|schedule|event|meeting|fecha|hora|cuando|inicio", re.I)


# -- record helpers ------------------------------------------------------------------
def unwrap(entry: Any) -> dict[str, Any]:
    """Record-map entries are {"value": {...}} or, in newer Notion builds, {"value": {"value": {...}, "role": ...}}."""
    if not isinstance(entry, dict):
        return {}
    v = entry.get("value", entry)
    if isinstance(v, dict) and isinstance(v.get("value"), dict) and "id" in v["value"]:
        v = v["value"]
    return v if isinstance(v, dict) else {}


def plain_text(rich: Any) -> str:
    """Notion rich text: [["text", [formatting...]], ...]."""
    if not isinstance(rich, list):
        return ""
    out = []
    for chunk in rich:
        if isinstance(chunk, list) and chunk and isinstance(chunk[0], str) and chunk[0] != "‣":
            out.append(chunk[0])
    return "".join(out).strip()


def parse_date_value(rich: Any) -> dict[str, Any] | None:
    """A date property value: [["‣", [["d", {start_date, start_time, end_date, end_time, time_zone, type}]]]]."""
    if not isinstance(rich, list):
        return None
    for chunk in rich:
        if not (isinstance(chunk, list) and len(chunk) > 1 and isinstance(chunk[1], list)):
            continue
        for fmt in chunk[1]:
            if isinstance(fmt, list) and len(fmt) > 1 and fmt[0] == "d" and isinstance(fmt[1], dict):
                d = fmt[1]
                start_date = d.get("start_date")
                if not start_date:
                    continue
                start_time, end_date, end_time = d.get("start_time"), d.get("end_date"), d.get("end_time")
                timed = bool(start_time) and d.get("type", "") in ("datetime", "datetimerange", "")
                if timed:
                    start = f"{start_date}T{start_time}"
                    end = f"{end_date or start_date}T{end_time}" if end_time else ""
                else:
                    start, end = start_date, (end_date or "")
                return {"start": start, "end": end, "all_day": not timed, "time_zone": d.get("time_zone") or ""}
    return None


def page_url(block_id: str) -> str:
    return "https://app.notion.com/" + block_id.replace("-", "")


def _table(rm: dict[str, Any], name: str) -> dict[str, dict[str, Any]]:
    return {k: unwrap(v) for k, v in (rm.get(name) or {}).items()}


# -- discovery ------------------------------------------------------------------------
async def _workspace_names(browser: Any, page: Any, uid: str, rm: dict[str, Any], space_ids: list[str]) -> dict[str, str]:
    names = {sid: (_table(rm, "space").get(sid) or {}).get("name", "") for sid in space_ids}
    missing = [sid for sid, n in names.items() if not n]
    if missing:
        try:
            res = await browser.api(page, "getPublicSpaceData", {"type": "space-ids", "spaceIds": missing,
                                                           "includeCounts": False}, user_id=uid)
            found = res.get("results") if isinstance(res, dict) else res
            for r in found if isinstance(found, list) else []:
                if isinstance(r, dict) and r.get("id") in names and r.get("name"):
                    names[r["id"]] = r["name"]
        except Exception:  # noqa: BLE001 - a name is cosmetic
            pass
    return {sid: (n or f"Workspace {sid[:8]}") for sid, n in names.items()}


async def _roots(browser: Any, page: Any, uid: str, sid: str, rm: dict[str, Any], space_view: dict[str, Any]) -> set[str]:
    """Top-level pages you can see in a workspace: private, shared, teamspace, and workspace pages."""
    roots = set(space_view.get("private_pages") or []) | set(space_view.get("bookmarked_pages") or [])
    roots |= {b["id"] for b in _table(rm, "block").values()
              if b.get("space_id") == sid and b.get("parent_table") in ("space", "team") and b.get("alive", True)}
    shared = await browser.api(page, "getUserSharedPagesInSpace", {"spaceId": sid}, user_id=uid, space_id=sid)
    roots |= set(shared.get("pages") or [])
    teams = await browser.api(page, "getTeamsV2", {"spaceId": sid, "teamTypes": ["Joined"],
                                             "includeMembershipSummary": False, "targetUserId": uid},
                        user_id=uid, space_id=sid)
    for team in _table(teams.get("recordMap") or {}, "team").values():
        roots |= set(team.get("team_pages") or [])
    return roots


async def _walk(browser: Any, page: Any, uid: str, sid: str, roots: set[str]) -> tuple[dict[str, dict[str, Any]], int]:
    """Visit pages breadth-first; return the databases found ({collection_id: {...}}) and the pages visited."""
    dbs: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    queue = deque(sorted(roots))
    while queue and len(seen) < MAX_PAGES_PER_WORKSPACE:
        pid = queue.popleft()
        if pid in seen:
            continue
        seen.add(pid)
        cursor: dict[str, Any] = {"stack": []}
        for _ in range(MAX_CHUNKS_PER_PAGE):
            chunk = await browser.api(page, "loadCachedPageChunkV2", {"page": {"id": pid}, "cursor": cursor,
                                                                "verticalColumns": False}, user_id=uid)
            rm = chunk.get("recordMap") or {}
            for b in _table(rm, "block").values():
                if not b.get("alive", True) or b.get("space_id", sid) != sid:
                    continue
                kind = b.get("type")
                if kind in ("collection_view", "collection_view_page") and b.get("collection_id"):
                    db = dbs.setdefault(b["collection_id"], {"views": set(), "collection": {}, "view_records": {}})
                    db["views"] |= set(b.get("view_ids") or [])
                    db["collection"] = db["collection"] or _table(rm, "collection").get(b["collection_id"], {})
                    db["view_records"].update({k: v for k, v in _table(rm, "collection_view").items() if v})
                if kind in ("page", "collection_view_page") and b["id"] != pid and b.get("parent_table") == "block":
                    queue.append(b["id"])
            cursor = chunk.get("cursor") or {}
            if not cursor.get("stack"):
                break
    return dbs, len(seen)


async def _records(browser: Any, page: Any, uid: str, sid: str, table: str, ids: list[str]) -> dict[str, dict[str, Any]]:
    if not ids:
        return {}
    res = await browser.api(page, "syncRecordValuesMain", {"requests": [
        {"pointer": {"table": table, "id": i, "spaceId": sid}, "version": -1} for i in ids]}, user_id=uid)
    return _table(res.get("recordMap") or {}, table)


def date_property(schema: dict[str, Any], views: list[dict[str, Any]], only_calendar_views: bool) -> str:
    """The property holding a row's date: the one a calendar/timeline view uses, else the best-named one."""
    dates = [k for k, v in schema.items() if (v or {}).get("type") == "date"]
    for view in views:
        if view.get("type") in ("calendar", "timeline"):
            fmt = view.get("format") or {}
            for key in ("calendar_by", "timeline_by"):
                if fmt.get(key) in dates:
                    return fmt[key]
    if only_calendar_views or not dates:
        return ""
    named = [k for k in dates if PREFERRED_DATE_NAMES.search((schema[k] or {}).get("name", ""))]
    return (named or dates)[0]


async def _query_rows(browser: Any, page: Any, uid: str, sid: str, cid: str, view_id: str,
                tzname: str) -> tuple[list[str], dict[str, dict[str, Any]], bool]:
    body = {"collection": {"id": cid, "spaceId": sid}, "collectionView": {"id": view_id, "spaceId": sid},
            "loader": {"type": "reducer",
                       "reducers": {"collection_group_results": {"type": "results", "limit": ROW_LIMIT}},
                       "sort": [], "searchQuery": "", "userTimeZone": tzname}}
    res = await browser.api(page, "queryCollection", body, user_id=uid, space_id=sid)
    group = ((res.get("result") or {}).get("reducerResults") or {}).get("collection_group_results") or {}
    return list(group.get("blockIds") or []), _table(res.get("recordMap") or {}, "block"), bool(group.get("hasMore"))


# -- extraction -----------------------------------------------------------------------
async def collect(browser: Any, page: Any, result: Any, lookahead_days: int, only_calendar_views: bool,
            tzname: str) -> None:
    """Fill `result` with every dated database row in range, from every workspace of every signed-in account."""
    from steward.notion_sync import NeedLoginSignal, NotionItem

    today = date.today()
    horizon = today + timedelta(days=lookahead_days)
    accounts = await browser.api(page, "getSpaces", {})
    seen_rows: set[str] = set()
    seen_spaces: set[str] = set()
    for uid, rm in accounts.items():
        if not isinstance(rm, dict):
            continue
        views = [v for v in _table(rm, "space_view").values() if v.get("alive", True) and v.get("space_id")]
        names = await _workspace_names(browser, page, uid, rm, [v["space_id"] for v in views])
        for sv in views:
            sid = sv["space_id"]
            if sid in seen_spaces:
                continue
            seen_spaces.add(sid)
            ws: dict[str, Any] = {"id": sid, "name": names[sid], "items": 0, "databases": 0, "pages": 0, "error": ""}
            result.workspaces.append(ws)
            try:
                roots = await _roots(browser, page, uid, sid, rm, sv)
                dbs, ws["pages"] = await _walk(browser, page, uid, sid, roots)
                for cid, db in dbs.items():
                    coll = db["collection"] or (await _records(browser, page, uid, sid, "collection", [cid])).get(cid, {})
                    if not coll or not coll.get("alive", True):
                        continue
                    missing = [v for v in db["views"] if v not in db["view_records"]]
                    db["view_records"].update(await _records(browser, page, uid, sid, "collection_view", missing))
                    view_list = [db["view_records"][v] for v in sorted(db["views"]) if v in db["view_records"]]
                    prop = date_property(coll.get("schema") or {}, view_list, only_calendar_views)
                    if not prop or not view_list:
                        continue
                    ws["databases"] += 1
                    ids, rows, more = await _query_rows(browser, page, uid, sid, cid, view_list[0]["id"], tzname)
                    db_name = plain_text(coll.get("name")) or "Untitled database"
                    if more:
                        ws.setdefault("notes", []).append(f"{db_name}: more than {ROW_LIMIT} rows; read the first {ROW_LIMIT}")
                    for rid in ids:
                        row = rows.get(rid) or {}
                        if rid in seen_rows or not row.get("alive", True) or row.get("is_template"):
                            continue
                        props = row.get("properties") or {}
                        when = parse_date_value(props.get(prop))
                        if not when:
                            continue
                        first = date.fromisoformat(when["start"][:10])
                        last = date.fromisoformat((when["end"] or when["start"])[:10])
                        if last < today or first > horizon:
                            continue
                        seen_rows.add(rid)
                        ws["items"] += 1
                        result.items.append(NotionItem(
                            id=rid, title=plain_text(props.get("title")) or "Untitled", start=when["start"],
                            end=when["end"], all_day=when["all_day"], time_zone=when["time_zone"],
                            workspace=ws["name"], database=db_name, url=page_url(rid)))
            except NeedLoginSignal:
                raise
            except Exception as exc:  # noqa: BLE001 - one workspace failing must not hide the others
                ws["error"] = f"{type(exc).__name__}: {exc}"[:300]
