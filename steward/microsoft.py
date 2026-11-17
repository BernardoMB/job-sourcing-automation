"""Microsoft 365 sign-in for the work calendar: Microsoft's own device-code sign-in, read-only calendar access.

You sign in on Microsoft's page (microsoft.com/devicelogin) with your work account, including its MFA and
company sign-in rules. Job Sourcer asks only for Calendars.Read, uses the app registration whose client ID
you put in Setup, and keeps the resulting token in secrets/ (never in steward.yaml or git). If your company
does not allow that app, Microsoft says so on the sign-in page: ask IT to approve it, or use a calendar link.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from steward.config import Config

SCOPES = "offline_access User.Read Calendars.Read"
GRAPH = "https://graph.microsoft.com/v1.0"


def _auth(cfg: Config) -> str:
    return f"https://login.microsoftonline.com/{cfg.work_calendar.graph_tenant or 'organizations'}/oauth2/v2.0"


def _client_id(cfg: Config) -> str:
    cid = (cfg.work_calendar.graph_client_id or "").strip()
    if not cid:
        raise RuntimeError("Put the app registration's Application (client) ID in Setup first")
    return cid


def _save(cfg: Config, token: dict[str, Any]) -> None:
    path = cfg.resolve(cfg.work_calendar.token_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(token), encoding="utf-8")


def _error(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return f"HTTP {resp.status_code}"
    return (data.get("error_description") or data.get("error") or f"HTTP {resp.status_code}").split("\r\n")[0]


def start_sign_in(cfg: Config) -> dict[str, Any]:
    """Ask Microsoft for a sign-in code. Returns the code, the page to enter it on, and what finish_sign_in needs."""
    resp = httpx.post(f"{_auth(cfg)}/devicecode", data={"client_id": _client_id(cfg), "scope": SCOPES}, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"Microsoft refused to start the sign-in: {_error(resp)}")
    flow = resp.json()
    return {"user_code": flow["user_code"], "verification_uri": flow["verification_uri"],
            "message": flow.get("message", ""), "device_code": flow["device_code"],
            "interval": int(flow.get("interval", 5)), "expires_at": time.time() + int(flow.get("expires_in", 900))}


def finish_sign_in(cfg: Config, flow: dict[str, Any], sleep=time.sleep) -> dict[str, Any]:
    """Wait until you finish on Microsoft's page, then save the token. Returns the account that signed in."""
    interval = flow["interval"]
    while time.time() < flow["expires_at"]:
        resp = httpx.post(f"{_auth(cfg)}/token", timeout=30, data={
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code", "client_id": _client_id(cfg),
            "device_code": flow["device_code"]})
        data = resp.json() if resp.content else {}
        if resp.status_code < 400 and data.get("access_token"):
            token = {**data, "expires_at": time.time() + int(data.get("expires_in", 3600)) - 120}
            me = httpx.get(f"{GRAPH}/me", headers={"Authorization": f"Bearer {data['access_token']}"}, timeout=30)
            info = me.json() if me.status_code < 400 else {}
            token["account"] = info.get("userPrincipalName") or info.get("mail") or ""
            _save(cfg, token)
            return {"account": token["account"]}
        error = data.get("error", "")
        if error == "authorization_pending":
            sleep(interval)
            continue
        if error == "slow_down":
            interval += 5
            sleep(interval)
            continue
        raise RuntimeError(f"Microsoft sign-in did not complete: {_error(resp)}")
    raise RuntimeError("The sign-in code expired before you finished. Start again.")


def access_token(cfg: Config) -> str:
    """A valid Graph token, refreshed when it has expired."""
    path = cfg.resolve(cfg.work_calendar.token_file)
    if not path.exists():
        raise RuntimeError("Not signed in to Microsoft yet: press Sign in with Microsoft in Setup")
    token = json.loads(path.read_text(encoding="utf-8"))
    if token.get("expires_at", 0) > time.time() and token.get("access_token"):
        return token["access_token"]
    resp = httpx.post(f"{_auth(cfg)}/token", timeout=30, data={
        "grant_type": "refresh_token", "client_id": _client_id(cfg), "refresh_token": token.get("refresh_token", ""),
        "scope": SCOPES})
    if resp.status_code >= 400:
        raise RuntimeError(f"Microsoft sign-in expired: {_error(resp)}. Sign in again in Setup.")
    data = resp.json()
    token.update({**data, "expires_at": time.time() + int(data.get("expires_in", 3600)) - 120})
    _save(cfg, token)
    return token["access_token"]


def account(cfg: Config) -> str:
    path = cfg.resolve(cfg.work_calendar.token_file)
    if not path.exists():
        return ""
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("account", "")
    except ValueError:
        return ""
