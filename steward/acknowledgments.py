"""Application acknowledgments: "Thank you for applying, we will review your application."

Such an email needs nothing from you, so it is marked as read and moved to your Jobs label
without an AI call or a draft. Only a pure acknowledgment counts: one that also asks you to do
something (a video interview, an assessment, scheduling), turns you down, or makes an offer goes
through the normal flow, so a request is never filed away unseen.
"""

from __future__ import annotations

import re
from typing import Any

from steward import assessments

ACK = re.compile(
    r"thanks? (you )?for (applying|your (job )?application|submitting your application|your interest in (the|our|this) "
    r"(position|role|job|opening|vacancy))|"
    r"(we('ve| have)|we) (successfully )?received your (job )?application|"
    r"your application (to|for) [^.\n]{1,80}? (has been|was) (successfully )?(received|submitted|sent)|"
    r"your application (has been|was) (successfully )?(received|submitted|sent)|"
    r"application (received|submitted|confirmation)|"
    r"we will (carefully )?review your (application|profile|resume|cv)|"
    r"gracias por (tu|su) (postulaci[oó]n|aplicaci[oó]n|inter[eé]s en (la|el|nuestra|nuestro) "
    r"(vacante|puesto|posici[oó]n|oferta))|"
    r"(hemos|ya) recibido (tu|su) (postulaci[oó]n|aplicaci[oó]n|cv|curr[ií]culum)|"
    r"(tu|su) (postulaci[oó]n|aplicaci[oó]n) (ha sido|fue) (recibida|enviada|registrada)",
    re.IGNORECASE,
)
# Anything you would have to act on, or that changes where you stand: never filed silently.
NEEDS_YOU = re.compile(
    r"interview|entrevista|assessment|evaluaci[oó]n|\btest\b|prueba|challenge|video|schedul|agend|availability|"
    r"disponibilidad|calendly|complete (the|your|this|a)|please (reply|respond|send|provide|fill|complete|confirm)|"
    r"unfortunately|regret|not (be )?(moving|proceed)|lamentablemente|no continuar|\boffer\b|oferta de trabajo",
    re.IGNORECASE,
)


def is_application_ack(msg: dict[str, Any]) -> bool:
    """A company only confirming it received your application (and asking nothing of you)."""
    subject, body = msg.get("subject", "") or "", (msg.get("body_text", "") or "")[:4000]
    text = f"{subject}\n{body}"
    if msg.get("in_reply_to") or msg.get("has_calendar_invite") or msg.get("invites"):
        return False  # part of a conversation, or a meeting: not a plain acknowledgment
    if not ACK.search(text) or NEEDS_YOU.search(text):
        return False
    return assessments.detect(msg) is None
