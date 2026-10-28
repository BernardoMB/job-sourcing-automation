"""Reply on the channel the message came from.

An email gets an email reply and a LinkedIn message gets a LinkedIn reply. Mail
from LinkedIn itself (message and InMail notifications, job alerts, digests) is
never answered by email: the conversation is read and answered on LinkedIn.
"""

from __future__ import annotations

import re

LINKEDIN_DOMAIN = re.compile(r"@(?:[a-z0-9-]+\.)*linkedin\.com$", re.IGNORECASE)
# "Ana sent you a message", "You have a new message", "Message replied: ...", InMail, hit-reply@linkedin.com, ...
MESSAGE_NOTICE = re.compile(
    r"sent you a (new )?message|new message|messaged you|message replied|replied to your|inmail|"
    r"wants to (talk|chat)|is waiting for your (reply|response)",
    re.IGNORECASE,
)
MESSAGE_SENDERS = re.compile(r"^(messages-noreply|messaging-digest-noreply|inmail-hit-reply|hit-reply)@", re.IGNORECASE)


NO_REPLY = re.compile(r"^(no-?reply|do-?not-?reply|donotreply|mailer-daemon|postmaster|bounces?)[^@]*@", re.IGNORECASE)


def is_no_reply(address: str) -> bool:
    """An address nobody reads (noreply@, do-not-reply@, mailer-daemon@): never answered by email."""
    return bool(NO_REPLY.search((address or "").strip()))


def is_linkedin_address(address: str) -> bool:
    return bool(LINKEDIN_DOMAIN.search((address or "").strip()))


THREAD_LINK = re.compile(r"linkedin\.com/messaging/thread/([A-Za-z0-9_%=+\-]+)", re.IGNORECASE)


def linkedin_thread_ids(text: str) -> list[str]:
    """The conversation ids a LinkedIn notification links to (linkedin.com/messaging/thread/<id>/)."""
    out: list[str] = []
    for found in THREAD_LINK.findall(text or ""):
        tid = found.replace("%3D", "=").replace("%3d", "=")
        if tid not in out:
            out.append(tid)
    return out


def is_linkedin_message_notice(from_email: str, subject: str, body: str) -> bool:
    """An email telling you someone messaged you on LinkedIn (as opposed to a job alert or digest)."""
    if not is_linkedin_address(from_email):
        return False
    return bool(MESSAGE_SENDERS.search(from_email.strip()) or MESSAGE_NOTICE.search(f"{subject}\n{body[:2000]}"))
