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


# A rejection is news, not an acknowledgment, even when it starts with "thank you for applying".
REJECTED = re.compile(
    r"not (be )?(moving|move|going) forward|decided (not )?to (move forward|proceed|continue) with other|"
    r"(have|has) not been selected|not selected|regret to inform|other candidates|"
    r"lamentablemente no|no (continuar|avanzar)emos|no fue seleccionad",
    re.IGNORECASE,
)
# Sentences that only describe what may happen later, and boilerplate: they ask nothing of you now.
# "If your profile matches, we will reach out to schedule an interview" is still a plain acknowledgment.
LATER = re.compile(
    r"^\W*(if|should|in case|once|when|whenever|si|en caso|cuando)\b|\b(we|we'?ll|you'?ll|you|they|someone|our team|"
    r"the team|nos|te) (will|shall|may|might|would|could)\b|\bwe'll\b|\byou'll\b|will be in touch|will (reach out|"
    r"contact)|look(ing)? forward|do not reply|unattended mailbox|replies will not be read|no responda",
    re.IGNORECASE,
)
# Where an application platform pastes back what you submitted (your own CV text is not a request).
APPLICATION_COPY = re.compile(r"copy of your application|your application data|personal information|"
                              r"\bresume\b\s*:|\bcv\b\s*:", re.IGNORECASE)


CONDITIONAL = re.compile(r"^\W*(if|should|in case|si|en caso)\b", re.IGNORECASE)


def _own_text(text: str) -> str:
    """The email's own words: without the copy of your application some platforms paste back."""
    cut = APPLICATION_COPY.search(text)
    return text[:cut.start()] if cut else text


def _sentences(text: str) -> list[str]:
    # Join wrapped lines first: "we will reach out to\nyou to schedule" is one sentence.
    return re.split(r"(?<=[.!?])\s+", " ".join(text.split()))


def _requests(text: str) -> list[str]:
    """Sentences that ask something of you now (conditional, future, and boilerplate sentences skipped)."""
    return [s for s in _sentences(_own_text(text)) if NEEDS_YOU.search(s) and not LATER.search(s)]


def _rejected(text: str) -> bool:
    """Turned down: "we will not be moving forward" (not "if you are not selected, ...")."""
    return any(REJECTED.search(s) and not CONDITIONAL.search(s) for s in _sentences(_own_text(text)))


def is_application_ack(msg: dict[str, Any]) -> bool:
    """A company only confirming it received your application (and asking nothing of you now)."""
    subject, body = msg.get("subject", "") or "", (msg.get("body_text", "") or "")[:4000]
    text = f"{subject}\n{body}"
    if msg.get("in_reply_to") or msg.get("has_calendar_invite") or msg.get("invites"):
        return False  # part of a conversation, or a meeting: not a plain acknowledgment
    if not ACK.search(text) or _rejected(text) or _requests(text):
        return False
    return assessments.detect({**msg, "body_text": _own_text(body)}) is None
