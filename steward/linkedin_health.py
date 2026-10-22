"""Is LinkedIn usable? Checked when Job Sourcer starts, and from the Logs tab.

- Reading: the toolkit lists your recent conversations and reads the newest one
  (a real read, so "works" means it works).
- Sending: cannot be tried without messaging someone, so it is judged from what
  decides it: the extension connected and logged in, the send fix applied, Copilot
  or Autopilot, the toolkit's own business hours, today's message quota, and how
  the last real send went.
- Other actions (invites, profile visits, searches): their quotas for today.

The verdict goes to the Logs tab (one line, plus one per problem with what to do)
and stays as a blocker there until a later check finds LinkedIn usable.
"""

from __future__ import annotations

import asyncio
from typing import Any

from steward import activity, linkedin_bridge
from steward.mcp_hub import IntegrationDisabled, MCPHub, MCPToolError
from steward.timeutil import fmt_local, now_iso


def _quota(q: dict[str, Any] | None) -> tuple[int, int]:
    q = q or {}
    return int(q.get("dailyUsed") or 0), int(q.get("dailyCap") or 0)


def _last_send(db) -> dict[str, Any] | None:
    return db.query_one("SELECT * FROM executions WHERE action_type = 'send_linkedin' AND status IN "
                        "('success', 'handed_off', 'failed') ORDER BY updated_at DESC LIMIT 1")


def assess(cfg, db, probe: dict[str, Any]) -> dict[str, Any]:
    """Turn the toolkit's answers into: can it read, can it send, and what is in the way."""
    tz_name = cfg.profile.timezone
    status = probe.get("status") or {}
    problems: list[tuple[str, str]] = []  # (level, sentence with the fix)
    if not status.get("connected", True):
        problems.append(("error", "The extension is not connected to its bridge: open Microsoft Edge and check the "
                                  "extension popup says the bridge is connected."))
    if status and not status.get("loggedIn"):
        problems.append(("error", "LinkedIn is signed out in Edge: sign in to linkedin.com there."))
    read_ok = probe.get("threads") is not None and not probe.get("read_error")
    if probe.get("read_error"):
        problems.append(("error", f"Reading the inbox failed: {probe['read_error']}"))

    patched = linkedin_bridge.extension_patched()
    on_disk, running = linkedin_bridge.extension_version_on_disk(), str(status.get("extensionVersion") or "")
    stale = bool(on_disk and running and on_disk != running)
    if stale:
        problems.append(("error", f"Edge is still running the extension's version {running}, but version {on_disk} is "
                                  "installed (the send fix is not active yet): open edge://extensions, turn on "
                                  "Developer mode, and click Reload on LinkedIn Toolkit."))
    used, cap = _quota((status.get("quotas") or {}).get("message"))
    left = max(cap - used, 0) if cap else None
    send_notes = [("Autopilot: approved replies go out directly" if status.get("autopilot") else
                   "Copilot: each reply waits for your approval in the extension popup")] if status else []
    if status and not status.get("businessHours"):
        send_notes.append("outside the toolkit's own hours, so it refuses sends until they start")
    if left is not None:
        send_notes.append(f"{left} of {cap} messages left today")
    last = _last_send(db)
    if last:
        when = fmt_local(last["updated_at"], tz_name)
        send_notes.append(f"last reply sent {when}" if last["status"] in ("success", "handed_off")
                          else f"last attempt failed {when}")
    if patched is False:
        problems.append(("error", "The extension cannot send messages: apply patches/linkedin-toolkit-2.1.1 (see its "
                                  "README), then click the toolkit icon once."))
    if left == 0:
        problems.append(("warn", "Today's LinkedIn message quota is used up: replies wait until tomorrow."))
    can_send = bool(status.get("connected", True) and status.get("loggedIn")) and patched is not False and left != 0         and not stale
    other = [f"{name} {u}/{c}" for name, (u, c) in (
        ("invites", _quota((status.get("quotas") or {}).get("invite"))),
        ("profile visits", _quota((status.get("quotas") or {}).get("visit"))),
        ("searches", _quota((status.get("quotas") or {}).get("search")))) if c]
    return {"read_ok": read_ok, "can_send": can_send, "threads": probe.get("threads"),
            "messages_read": probe.get("messages_read"), "send_notes": send_notes, "other": other,
            "version": status.get("extensionVersion", ""), "patched": patched,
            "pending_in_toolkit": (status.get("queue") or {}).get("pending", 0), "problems": problems}


async def check(st, trigger: str = "startup") -> dict[str, Any]:
    """Run the check, log it, and remember the verdict for the Logs tab's blockers."""
    cfg, db = st.cfg, st.db
    if cfg.linkedin.backend != "upstream":
        what = "off (Setup step 6)" if cfg.linkedin.backend == "disabled" else f"{cfg.linkedin.backend} (demo data)"
        activity.record(db, "linkedin", f"LinkedIn check ({trigger}): LinkedIn is {what}.")
        db.kv_set("linkedin_health", {"at": now_iso(), "ok": cfg.linkedin.backend != "disabled", "problems": []})
        return {"backend": cfg.linkedin.backend}
    bridge = await asyncio.to_thread(linkedin_bridge.ensure_running, cfg)
    try:
        async with MCPHub(cfg) as hub:
            probe = await hub.call("linkedin", "health", {}, timeout=120)
        result = assess(cfg, db, probe)
    except (MCPToolError, IntegrationDisabled, OSError) as exc:
        note = bridge.get("note") or ""
        result = {"read_ok": False, "can_send": False, "problems": [("error", f"The LinkedIn toolkit did not answer: "
                                                                             f"{exc}{' ' + note if note else ''}")],
                  "send_notes": [], "other": []}
    reading = (f"reading works ({result['threads']} recent conversations, newest one read)" if result["read_ok"]
               else "reading does NOT work")
    sending = ("sending ready" if result["can_send"] else "sending NOT ready") + \
        (f" ({'; '.join(result['send_notes'])})" if result["send_notes"] else "")
    extra = []
    if result.get("version"):
        extra.append(f"extension {result['version']}" + (" with the send fix" if result.get("patched") else ""))
    if result.get("pending_in_toolkit"):
        extra.append(f"{result['pending_in_toolkit']} waiting in the toolkit's approval queue")
    if result["other"]:
        extra.append("other actions today: " + ", ".join(result["other"]))
    ok = result["read_ok"] and result["can_send"]
    level = "info" if ok else ("error" if not result["read_ok"] else "warn")
    activity.record(db, "linkedin", f"LinkedIn check ({trigger}): {reading} · {sending}"
                    + (" · " + " · ".join(extra) if extra else ""), level,
                    details={k: v for k, v in result.items() if k != "problems"})
    for lvl, sentence in result["problems"]:
        activity.record(db, "linkedin", f"LinkedIn: {sentence}", lvl)
    db.kv_set("linkedin_health", {"at": now_iso(), "ok": ok, "read_ok": result["read_ok"],
                                  "can_send": result["can_send"], "problems": [p for _, p in result["problems"]]})
    return result
