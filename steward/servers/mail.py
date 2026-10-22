"""Email MCP server: read via IMAP, send via SMTP (or a local sandbox mailbox).

Works with any IMAP/SMTP provider (Gmail with an App Password, Outlook,
Fastmail, ...). Reading uses BODY.PEEK so messages are never marked as read.
`send_email` is a write tool: only Job Sourcer's executor calls it, and only for
actions you approved or that an earned rule allows.
"""

from __future__ import annotations

import email
import imaplib
import os
import re
import smtplib
from datetime import timedelta
from email.message import EmailMessage
from email.policy import default as default_policy
from email.utils import formataddr, getaddresses, make_msgid, parseaddr, parsedate_to_datetime
from html import unescape
from typing import Any

from steward import invites
from steward.servers.common import READ_ONLY_REMOTE, WRITE, JsonStore, guarded, make_server, server_config
from steward.timeutil import now_iso, parse_iso, to_iso, utcnow

cfg = server_config()
mail_cfg = cfg.mail
mcp = make_server(
    "steward-mail",
    "Read recruiter email over IMAP and send replies over SMTP. send_email is reserved for the Job Sourcer executor.",
)


def _html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
    html = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", html)
    text = re.sub(r"<[^>]+>", "", html)
    return re.sub(r"\n{3,}", "\n\n", unescape(text)).strip()


def _strip_quoted(text: str) -> str:
    """Drop the quoted history below a reply so classification sees the new message."""
    out = []
    for line in text.splitlines():
        if re.match(r"^On .+ wrote:$", line.strip()) or line.strip().startswith("-----Original Message"):
            break
        if line.startswith(">"):
            continue
        out.append(line)
    return "\n".join(out).strip()


# -- sandbox backend ------------------------------------------------------
class SandboxMail:
    def __init__(self) -> None:
        base = cfg.sandbox_path
        self.inbox = JsonStore(base / "mailbox.json", {"messages": []})
        self.outbox = JsonStore(base / "outbox.json", {"sent": []})

    def address(self) -> str:
        return cfg.profile.email or "me@example.com"

    @staticmethod
    def _read(m: dict[str, Any]) -> dict[str, Any]:
        """Sandbox messages may carry raw `ics` text and `html`; read invites from them like real mail."""
        if "ics" not in m and "html" not in m:
            return m
        ics = m.get("ics") or []
        found = invites.extract([ics] if isinstance(ics, str) else ics, m.get("html", ""), m.get("body_text", ""),
                                cfg.profile.timezone)
        rest = {k: v for k, v in m.items() if k not in ("ics", "html")}
        return {**rest, "has_calendar_invite": bool(ics or found["invites"]), **found}

    def list(self, folder: str, since_uid: int, limit: int) -> list[dict[str, Any]]:
        msgs = [self._read(m) for m in self.inbox.load()["messages"] if int(m["uid"]) > since_uid]
        return sorted(msgs, key=lambda m: int(m["uid"]))[:limit]

    def get(self, uid: int, folder: str) -> dict[str, Any] | None:
        m = next((m for m in self.inbox.load()["messages"] if int(m["uid"]) == uid), None)
        return self._read(m) if m else None

    def search(self, query: str, folder: str, limit: int) -> list[dict[str, Any]]:
        q = query.lower()
        msgs = [m for m in self.inbox.load()["messages"]
                if q in (m.get("subject", "") + m.get("body_text", "") + m.get("from_email", "")).lower()]
        return msgs[:limit]

    def sent(self, since_uid: int, since_date: str) -> tuple[str, list[dict[str, Any]]]:
        """Mail you sent yourself (the sandbox keeps it under `sent_by_me` in mailbox.json)."""
        mine = self.inbox.load().get("sent_by_me", [])
        return "Sent", sorted((m for m in mine if int(m["uid"]) > since_uid), key=lambda m: int(m["uid"]))

    def send(self, msg: EmailMessage) -> None:
        data = self.outbox.load()
        data["sent"].append(
            {
                "message_id": msg["Message-ID"],
                "to": msg["To"],
                "cc": msg.get("Cc", ""),
                "subject": msg["Subject"],
                "in_reply_to": msg.get("In-Reply-To", ""),
                "body": msg.get_content(),
                "sent_at": now_iso(),
            }
        )
        self.outbox.save(data)


# -- IMAP/SMTP backend ----------------------------------------------------
class ImapMail:
    def __init__(self) -> None:
        self.password = os.environ.get(mail_cfg.password_env, "")
        if not mail_cfg.username or not self.password:
            raise RuntimeError(f"Set mail.username in steward.yaml and {mail_cfg.password_env} in .env")

    def address(self) -> str:
        return mail_cfg.username

    def _imap(self) -> imaplib.IMAP4_SSL:
        conn = imaplib.IMAP4_SSL(mail_cfg.imap_host, mail_cfg.imap_port)
        conn.login(mail_cfg.username, self.password)
        return conn

    @staticmethod
    def _quote(folder: str) -> str:
        return folder if folder.startswith('"') else f'"{folder}"'

    def _parse(self, uid: int, raw: bytes, folder: str) -> dict[str, Any]:
        msg = email.message_from_bytes(raw, policy=default_policy)
        body_part = msg.get_body(preferencelist=("plain", "html"))
        body = ""
        if body_part is not None:
            content = body_part.get_content()
            body = _html_to_text(content) if body_part.get_content_type() == "text/html" else content
        html_part = msg.get_body(preferencelist=("html",))
        html = html_part.get_content() if html_part is not None else ""
        ics_texts = []
        for part in msg.walk():
            name = (part.get_filename() or "").lower()
            if part.get_content_type() in ("text/calendar", "application/ics") or name.endswith(".ics"):
                payload = part.get_payload(decode=True) or b""
                ics_texts.append(payload.decode(part.get_content_charset() or "utf-8", errors="replace"))
        name, addr = parseaddr(str(msg.get("From", "")))
        try:
            date = to_iso(parsedate_to_datetime(str(msg.get("Date"))))
        except (TypeError, ValueError):
            date = now_iso()
        refs = str(msg.get("References", "") or "").split()
        found = invites.extract(ics_texts, html, body, cfg.profile.timezone)
        return {
            "uid": uid,
            "folder": folder,
            "message_id": str(msg.get("Message-ID", "") or f"<uid-{uid}@{mail_cfg.imap_host}>").strip(),
            "from_name": name,
            "from_email": addr.lower(),
            "to": [a for _, a in getaddresses([str(msg.get("To", ""))])],
            "cc": [a for _, a in getaddresses([str(msg.get("Cc", ""))])],
            "subject": str(msg.get("Subject", "") or ""),
            "date": date,
            "body_text": _strip_quoted(body)[:20000],
            "in_reply_to": str(msg.get("In-Reply-To", "") or "").strip(),
            "references": refs,
            "has_calendar_invite": bool(ics_texts or found["invites"]),
            "invites": found["invites"],
            "add_to_calendar": found["add_to_calendar"],
            "links": found["links"],
        }

    def list(self, folder: str, since_uid: int, limit: int) -> list[dict[str, Any]]:
        conn = self._imap()
        try:
            conn.select(self._quote(folder), readonly=True)
            if since_uid <= 0:
                since = (utcnow() - timedelta(days=mail_cfg.initial_lookback_days)).strftime("%d-%b-%Y")
                typ, data = conn.uid("SEARCH", None, "SINCE", since)
            else:
                typ, data = conn.uid("SEARCH", None, "UID", f"{since_uid + 1}:*")
            uids = [int(u) for u in (data[0] or b"").split() if int(u) > since_uid]
            uids = sorted(uids)[-limit:]
            out = []
            for uid in uids:
                typ, parts = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
                raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
                if raw:
                    out.append(self._parse(uid, raw, folder))
            return out
        finally:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass

    def get(self, uid: int, folder: str) -> dict[str, Any] | None:
        conn = self._imap()
        try:
            conn.select(self._quote(folder), readonly=True)
            typ, parts = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
            raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
            return self._parse(uid, raw, folder) if raw else None
        finally:
            conn.logout()

    def search(self, query: str, folder: str, limit: int) -> list[dict[str, Any]]:
        conn = self._imap()
        try:
            conn.select(self._quote(folder), readonly=True)
            safe = query.replace('"', "")
            typ, data = conn.uid("SEARCH", "CHARSET", "UTF-8", "TEXT", f'"{safe}"')
            uids = sorted(int(u) for u in (data[0] or b"").split())[-limit:]
            out = []
            for uid in uids:
                typ, parts = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
                raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
                if raw:
                    out.append(self._parse(uid, raw, folder))
            return out
        finally:
            conn.logout()

    def _sent_folder(self, conn: imaplib.IMAP4_SSL) -> str:
        if mail_cfg.sent_folder:
            return mail_cfg.sent_folder
        typ, rows = conn.list()
        for row in rows or []:
            line = row.decode("utf-8", errors="replace") if isinstance(row, bytes) else str(row)
            if "\\Sent" in line:
                m = re.search(r'"([^"]+)"\s*$', line) or re.search(r"(\S+)\s*$", line)
                if m:
                    return m.group(1)
        return "[Gmail]/Sent Mail" if "gmail" in mail_cfg.imap_host else "Sent"

    def sent(self, since_uid: int, since_date: str) -> tuple[str, list[dict[str, Any]]]:
        """Headers of mail you sent (newest UIDs first fetched), to notice replies written outside Job Sourcer."""
        conn = self._imap()
        try:
            folder = self._sent_folder(conn)
            conn.select(self._quote(folder), readonly=True)
            if since_uid <= 0:
                typ, data = conn.uid("SEARCH", None, "SINCE", since_date)
            else:
                typ, data = conn.uid("SEARCH", None, "UID", f"{since_uid + 1}:*")
            uids = sorted(int(u) for u in (data[0] or b"").split() if int(u) > since_uid)[-200:]
            out = []
            for uid in uids:
                typ, parts = conn.uid("FETCH", str(uid), "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID TO CC SUBJECT DATE "
                                                       "IN-REPLY-TO REFERENCES)])")
                raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
                if not raw:
                    continue
                h = email.message_from_bytes(raw, policy=default_policy)
                try:
                    date = to_iso(parsedate_to_datetime(str(h.get("Date"))))
                except (TypeError, ValueError):
                    date = now_iso()
                out.append({"uid": uid, "message_id": str(h.get("Message-ID", "") or "").strip(),
                            "to": [a.lower() for _, a in getaddresses([str(h.get("To", "")), str(h.get("Cc", ""))])],
                            "subject": str(h.get("Subject", "") or ""), "date": date,
                            "in_reply_to": str(h.get("In-Reply-To", "") or "").strip(),
                            "references": str(h.get("References", "") or "").split()})
            return folder, out
        finally:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass

    def send(self, msg: EmailMessage) -> None:
        if mail_cfg.smtp_starttls:
            with smtplib.SMTP(mail_cfg.smtp_host, mail_cfg.smtp_port, timeout=60) as s:
                s.starttls()
                s.login(mail_cfg.username, self.password)
                s.send_message(msg)
        else:
            with smtplib.SMTP_SSL(mail_cfg.smtp_host, mail_cfg.smtp_port, timeout=60) as s:
                s.login(mail_cfg.username, self.password)
                s.send_message(msg)
        if mail_cfg.save_sent_folder:
            conn = self._imap()
            try:
                conn.append(self._quote(mail_cfg.save_sent_folder), "\\Seen", None, msg.as_bytes())
            finally:
                conn.logout()


_backend: SandboxMail | ImapMail | None = None


def backend() -> SandboxMail | ImapMail:
    global _backend
    if _backend is None:
        if mail_cfg.backend == "imap":
            _backend = ImapMail()
        elif mail_cfg.backend == "sandbox":
            _backend = SandboxMail()
        else:
            raise RuntimeError("Email integration is disabled in steward.yaml")
    return _backend


@mcp.tool(annotations=READ_ONLY_REMOTE)
@guarded
def whoami() -> dict[str, Any]:
    """The mailbox address Job Sourcer reads and sends from."""
    return {"address": backend().address(), "backend": mail_cfg.backend, "folder": mail_cfg.folder}


@mcp.tool(annotations=READ_ONLY_REMOTE)
@guarded
def list_messages(folder: str = "", since_uid: int = 0, limit: int = 40) -> dict[str, Any]:
    """New messages with UID greater than `since_uid` (oldest first). Never marks messages as read."""
    msgs = backend().list(folder or mail_cfg.folder, since_uid, min(limit, mail_cfg.max_per_poll))
    last = max([since_uid, *[int(m["uid"]) for m in msgs]])
    return {"messages": msgs, "last_uid": last}


@mcp.tool(annotations=READ_ONLY_REMOTE)
@guarded
def list_sent(since_uid: int = 0, since: str = "") -> dict[str, Any]:
    """Headers of mail you sent (from your Sent folder) with UID greater than `since_uid`, or since a date when
    `since_uid` is 0. Used to notice replies you wrote yourself."""
    day = (parse_iso(since) or (utcnow() - timedelta(days=mail_cfg.initial_lookback_days))).strftime("%d-%b-%Y")
    folder, msgs = backend().sent(since_uid, day)
    return {"folder": folder, "messages": msgs, "last_uid": max([since_uid, *[int(m["uid"]) for m in msgs]])}


@mcp.tool(annotations=READ_ONLY_REMOTE)
@guarded
def get_message(uid: int, folder: str = "") -> dict[str, Any]:
    """Fetch one message by UID."""
    msg = backend().get(uid, folder or mail_cfg.folder)
    if msg is None:
        raise ValueError(f"No message with UID {uid}")
    return msg


@mcp.tool(annotations=READ_ONLY_REMOTE)
@guarded
def search_messages(query: str, folder: str = "", limit: int = 20) -> dict[str, Any]:
    """Full-text search in a folder; useful to pull earlier messages of a thread."""
    return {"messages": backend().search(query, folder or mail_cfg.folder, limit)}


@mcp.tool(annotations=WRITE)
@guarded
def send_email(
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    in_reply_to: str = "",
    references: list[str] | None = None,
) -> dict[str, Any]:
    """Send a plain-text email. Sets In-Reply-To/References so the reply stays in the recruiter's thread."""
    if not to:
        raise ValueError("send_email needs at least one recipient")
    b = backend()
    msg = EmailMessage()
    sender = b.address()
    msg["From"] = formataddr((mail_cfg.from_name or cfg.profile.name, sender))
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    domain = sender.split("@")[-1] if "@" in sender else "steward.local"
    msg["Message-ID"] = make_msgid(domain=domain)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        refs = list(references or [])
        if in_reply_to not in refs:
            refs.append(in_reply_to)
        msg["References"] = " ".join(refs)
    msg.set_content(body)
    b.send(msg)
    return {"status": "sent", "message_id": msg["Message-ID"], "to": to, "sent_at": now_iso()}


if __name__ == "__main__":
    mcp.run()
