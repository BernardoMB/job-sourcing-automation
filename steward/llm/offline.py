"""Offline heuristic provider: no API calls, deterministic, for demos and tests.

It implements the same structured contracts as the real providers using
keyword rules and templates. It is deliberately simple and is NOT meant for real
recruiter conversations; set `llm.provider: anthropic` (or `grok`) for that.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from steward.config import Config
from steward.timeutil import fmt_local, parse_iso, to_iso, tz
from steward.vocab import find_technologies

RECRUITING_WORDS = re.compile(
    r"\b(job|role|position|opportunit|recruit|hiring|interview|candidate|talent|offer|salary|compensation|"
    r"resume|cv|headcount|team is growing|hiring manager|screen|vacante|reclut|entrevista|puesto)",
    re.IGNORECASE,
)
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december"]
TIME_RE = re.compile(
    r"(?:(monday|tuesday|wednesday|thursday|friday|saturday|sunday),?\s+)?"
    r"(january|february|march|april|may|june|july|august|september|october|november|december)\s+"
    r"(\d{1,2})(?:st|nd|rd|th)?(?:,?\s*\d{4})?\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)",
    re.IGNORECASE,
)

CALL_RE = re.compile(r"\b(voicemail|missed call|missed a call)\b", re.I)
PHONE_RE = re.compile(r"\+?\d[\d\s().-]{8,}\d")

URL_RE = re.compile(r"https?://\S+")
DUE_RE = re.compile(
    r"\b(?:by|before|until|no later than|deadline:?)\s+(?:(?:monday|tuesday|wednesday|thursday|friday|saturday|"
    r"sunday),?\s+)?(january|february|march|april|may|june|july|august|september|october|november|december)\s+"
    r"(\d{1,2})", re.IGNORECASE)
TASK_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("assessment", re.compile(r"\b(assessment|coding challenge|take-home|hackerrank|codility|codesignal|online test)",
                              re.I)),
    ("form", re.compile(r"\b(form|questionnaire|survey)\b", re.I)),
    ("application", re.compile(r"\b(apply (here|at|via|through|using)|application link|submit (your|an) application)",
                               re.I)),
    ("document", re.compile(r"\b(portfolio|cover letter|writing sample|references list)\b", re.I)),
    ("prepare", re.compile(r"\b(prepare a|presentation for)\b", re.I)),
]
DECLINE_RE = re.compile(r"\b(decline|not interested|pass on|turn (it|this) down|withdraw)", re.I)
TASK_TITLES = {
    "assessment": "Complete the online assessment",
    "form": "Fill in the form",
    "application": "Apply through the link",
    "document": "Prepare and send the requested document",
    "prepare": "Prepare what they asked for",
}

CATEGORY_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("offer", re.compile(r"\b(extend (you )?an offer|offer letter|pleased to offer|formal offer|verbal offer)", re.I)),
    ("negotiation", re.compile(r"\bnegotiat", re.I)),
    ("salary_question", re.compile(r"\b(salary|compensation|pay range|base pay|rate expectations|total comp)", re.I)),
    ("relocation_question", re.compile(r"\b(relocat|on-?site|in[- ]office|willing to move|commute)", re.I)),
    ("work_authorization", re.compile(r"\b(visa|sponsorship|authori[sz]ed to work|work permit|right to work)", re.I)),
    ("notice_period", re.compile(r"\b(notice period|start date|when could you start|available to start)", re.I)),
    ("references_background", re.compile(r"\b(references|background check|social security|passport)", re.I)),
    ("rejection", re.compile(r"\b(unfortunately|move forward with other|position has been filled|not moving forward)", re.I)),
    ("experience_question", re.compile(r"\b(how many years|years of experience|led a team|managed a team)", re.I)),
    ("logistics", re.compile(r"\b(interview process|how to prepare|interview format|next round)", re.I)),
    ("availability_request", re.compile(
        r"\b(when are you (free|available)|your availability|availability|schedule a (call|chat)|find a time|"
        r"times? that work|open to a (quick )?(chat|call))", re.I)),
    ("role_pitch", re.compile(r"\b(open to|opportunit|role|position|hiring|team is growing)", re.I)),
]

QUESTION_TYPE_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("salary", re.compile(r"\b(salary|compensation|pay|expectations)", re.I)),
    ("relocation", re.compile(r"\b(relocat|on-?site|in[- ]office|willing to move)", re.I)),
    ("work_authorization", re.compile(r"\b(visa|sponsorship|authori[sz]ed)", re.I)),
    ("notice_period", re.compile(r"\b(notice|start date|start)", re.I)),
    ("availability", re.compile(r"\b(free|available|availability|time|call|chat)\b", re.I)),
    ("years_experience", re.compile(r"\b(how many years|years)", re.I)),
    ("interest", re.compile(r"\b(open to|interested)", re.I)),
    ("logistics", re.compile(r"\b(process|prepare|format)", re.I)),
]


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.?!])\s+|\n+", text)
    return [p.strip() for p in parts if p.strip()]


def _first_name(name: str) -> str:
    return (name or "").strip().split(" ")[0] if name else "there"


class OfflineLLM:
    name = "offline"

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def model_for(self, purpose: str) -> str:
        return "offline-heuristics"

    def json(self, purpose: str, system: str, user: str, schema: dict[str, Any],
             context: dict[str, Any] | None = None) -> dict[str, Any]:
        ctx = context or {}
        handler = getattr(self, f"_{purpose}", None)
        if handler is None:
            raise ValueError(f"offline provider has no handler for {purpose}")
        return handler(ctx)

    # -- classify -------------------------------------------------------
    def _classify(self, ctx: dict[str, Any]) -> dict[str, Any]:
        ev = ctx.get("event", {})
        text = f"{ev.get('subject', '')}\n{ev.get('body_text', '')}"
        source = ev.get("source", "email")
        recruiter = bool(RECRUITING_WORDS.search(text)) or source in ("linkedin", "calendar")
        if re.search(r"\b(unsubscribe|newsletter|weekly digest)\b", text, re.I) and not re.search(
            r"\b(role|position)\b", text, re.I
        ):
            recruiter = False
        categories: list[str] = []
        if source == "calendar":
            categories.append("calendar_invite")
        for cat, pat in CATEGORY_RULES:
            if pat.search(text) and cat not in categories:
                categories.append(cat)
        times = self._parse_times(text, ctx)
        if times:
            categories = [c for c in categories if c != "availability_request"]
            if "interview_request" not in categories and source != "calendar":
                categories.insert(0, "interview_request")
        techs = find_technologies(text, self.cfg.cv.aliases)
        questions = []
        for sent in _sentences(ev.get("body_text", "")):
            if not sent.endswith("?"):
                continue
            q_techs = find_technologies(sent, self.cfg.cv.aliases)
            qtype = "other"
            if TIME_RE.search(sent):
                qtype = "availability"
            elif q_techs:
                qtype = "years_experience" if re.search(r"\byears\b", sent, re.I) else "tech_experience"
            else:
                for t, pat in QUESTION_TYPE_RULES:
                    if pat.search(sent):
                        qtype = t
                        break
            terms = q_techs or [w for w in re.findall(r"[A-Za-z][A-Za-z+#.]{3,}", sent) if w.lower() not in {
                "would", "could", "your", "with", "have", "what", "when", "this", "that", "about", "there"}][:4]
            questions.append({"text": sent, "type": qtype, "entities": q_techs, "search_terms": terms})
        if any(q["type"] in ("tech_experience",) for q in questions) and "tech_question" not in categories:
            categories.insert(0, "tech_question")
        if any(q["type"] == "years_experience" for q in questions) and "experience_question" not in categories:
            categories.append("experience_question")
        call = bool(CALL_RE.search(text))
        if call and "call_notification" not in categories:
            categories.insert(0, "call_notification")
            categories = [c for c in categories if c not in ("availability_request",)]
        if not recruiter:
            categories = ["not_recruiter"]
        if not categories:
            categories = ["other"]
        stage = "none"
        if "offer" in categories or "negotiation" in categories:
            stage = "offer"
        elif "rejection" in categories:
            stage = "rejection"
        elif re.search(r"\bfinal round\b", text, re.I):
            stage = "final_round"
        elif re.search(r"\b(technical interview|onsite interview|coding interview)\b", text, re.I):
            stage = "interviewing"
        elif "interview_request" in categories or "availability_request" in categories:
            stage = "screening"
        elif "role_pitch" in categories:
            stage = "new_opportunity"
        company = ""
        m = re.search(r"\b(?:at|with|from|join) ((?:[A-Z][\w&]+)(?: [A-Z][\w&]+){0,2})", ev.get("body_text", ""))
        if m:
            company = m.group(1).strip()
        role = ""
        m = re.search(
            r"((?:Senior|Staff|Lead|Principal|Junior)?\s?(?:[A-Z][a-z]+ ){0,2}(?:Engineer|Developer|Architect|"
            r"Manager|Scientist|SRE))",
            text,
        )
        if m:
            role = m.group(1).strip()
        primary = categories[0]
        phone = PHONE_RE.search(ev.get("body_text", ""))
        caller = re.search(r"(?:voicemail|call) from ([A-Z][a-z]+(?: [A-Z][a-z]+)?)", text) if call else None
        return {
            "is_recruiter_related": recruiter,
            "needs_reply": recruiter and source != "calendar",
            "summary": (ev.get("subject") or ev.get("body_text", "")[:80]).strip(),
            "company": company,
            "role": role,
            "recruiter_name": caller.group(1) if caller else ev.get("sender_name", ""),
            "phone_number": phone.group(0).strip() if phone else "",
            "location": "",
            "categories": categories,
            "primary_category": primary,
            "questions": questions,
            "technologies": techs,
            "proposed_times": times,
            "meeting_link": "",
            "stage_signal": stage,
            "urgency": "high" if primary in ("offer", "interview_request") else "normal",
            "suspicious": False,
            "candidate_tasks": self._tasks(ev.get("body_text", ""), company, ctx) if recruiter else [],
        }

    def _tasks(self, body: str, company: str, ctx: dict[str, Any]) -> list[dict[str, str]]:
        out, seen = [], set()
        link = URL_RE.search(body)
        due = self._parse_due(body, ctx)
        for kind, pat in TASK_RULES:
            m = pat.search(body)
            if not m or kind in seen:
                continue
            seen.add(kind)
            sentence = next((s for s in _sentences(body) if pat.search(s)), m.group(0))
            out.append({"title": TASK_TITLES[kind] + (f" ({company})" if company else ""), "kind": kind,
                        "due": due, "link": link.group(0).rstrip(").,") if link else "", "details": sentence[:300]})
        return out

    def _parse_due(self, text: str, ctx: dict[str, Any]) -> str:
        now = parse_iso(ctx.get("now")) or datetime.now(tz(self.cfg.profile.timezone))
        zone = tz(ctx.get("timezone") or self.cfg.profile.timezone)
        m = re.search(r"within (\d+) (hours?|days?)", text, re.I)
        if m:
            n = int(m.group(1))
            return to_iso(now + (timedelta(hours=n) if m.group(2).lower().startswith("hour") else timedelta(days=n)))
        m = DUE_RE.search(text)
        if m:
            month = MONTHS.index(m.group(1).lower()) + 1
            try:
                due = datetime(now.astimezone(zone).year, month, int(m.group(2)), 23, 59, tzinfo=zone)
            except ValueError:
                return ""
            if due < now - timedelta(days=1):
                due = due.replace(year=due.year + 1)
            return to_iso(due)
        return ""

    def _parse_times(self, text: str, ctx: dict[str, Any]) -> list[dict[str, str]]:
        now = parse_iso(ctx.get("now")) or datetime.now(tz(self.cfg.profile.timezone))
        zone = tz(ctx.get("timezone") or self.cfg.profile.timezone)
        out = []
        for m in TIME_RE.finditer(text):
            month = MONTHS.index(m.group(2).lower()) + 1
            day = int(m.group(3))
            hour = int(m.group(4)) % 12 + (12 if m.group(6).lower() == "pm" else 0)
            minute = int(m.group(5) or 0)
            year = now.astimezone(zone).year
            try:
                start = datetime(year, month, day, hour, minute, tzinfo=zone)
            except ValueError:
                continue
            if start < now - timedelta(days=1):
                start = start.replace(year=year + 1)
            out.append({"start": to_iso(start), "end": to_iso(start + timedelta(minutes=30))})
        return out

    # -- draft ----------------------------------------------------------
    def _draft(self, ctx: dict[str, Any]) -> dict[str, Any]:
        ev = ctx.get("event", {})
        cls = ctx.get("classification", {})
        evidence = ctx.get("evidence", {})
        sched = ctx.get("schedule", {})
        profile = self.cfg.profile
        name = _first_name(cls.get("recruiter_name") or ev.get("sender_name", ""))
        company = cls.get("company") or "your team"
        role = cls.get("role") or "the role"
        cats = set(cls.get("categories", []))
        lines = [f"Hi {name},", ""]
        answered, unanswered, commitments = [], [], []
        note = (ctx.get("instruction") or "").split("\nPlan:")[0].strip()
        if note and DECLINE_RE.search(note):
            lines += [f"Thank you for thinking of me for the {role} role at {company}. After some consideration, "
                      "I'll pass on this one for now, but I'd be glad to stay in touch about future roles.",
                      "", "Best,", profile.signature or profile.name]
            return {"reply_subject": f"Re: {ev.get('subject') or ''}".strip(), "reply_body": "\n".join(lines),
                    "answered_questions": [], "unanswered_questions": [], "commitments": [],
                    "next_step": "stay in touch", "confidence": 0.9, "rationale": "Declined as you instructed."}
        if note:
            lines.append(note[0].upper() + note[1:].rstrip(".") + ".")
        if "call_notification" in cats:
            lines.append(f"Sorry I missed your call! Thanks for reaching out about the {role} opportunity at "
                         f"{company} - I'd love to talk.")
        elif "meeting_change" in cats:
            lines.append(f"I saw our meeting about the {role} role changed - no problem at all. I'm still very "
                         "interested and happy to find another time.")
        elif "follow_up" in cats:
            lines.append(f"I wanted to follow up on the {role} opportunity at {company}. I'm still very interested "
                         "and would love to hear about next steps.")
        elif "rejection" in cats:
            lines.append(f"Thank you for letting me know, and for your time throughout the process. I'd be glad to "
                         f"be considered for future roles at {company}.")
        elif "offer" in cats:
            lines.append(f"Thank you so much - I'm excited about the offer for the {role} position. I'll review the "
                         "details carefully and get back to you shortly.")
        else:
            lines.append(f"Thanks for reaching out about the {role} position at {company} - it sounds like a great fit "
                         "for what I'm looking for.")
        for item in evidence.get("items", []):
            q = item.get("question", "")
            if item.get("answer_found") and item.get("skill"):
                yrs = item.get("years")
                where = (item.get("roles") or [{}])[0].get("title", "")
                yrs_txt = f"{yrs:g} years of" if yrs else "solid"
                lines.append(f"I have {yrs_txt} hands-on experience with {item['skill']}"
                             + (f", most recently as {where}." if where else "."))
                answered.append(q)
            elif item.get("answer_found") and item.get("source") == "profile":
                lines.append(item.get("answer", ""))
                answered.append(q)
            elif item.get("answer_found") and item.get("evidence"):
                lines.append(f"Relevant to your question: {item['evidence'][0]['text']}")
                answered.append(q)
            elif item.get("type") not in ("salary", "availability"):
                unanswered.append(q)
                lines.append(f"[NEEDS YOUR INPUT: {q}]")
        if "salary_question" in cats:
            lines.append("On compensation, I'd like to learn more about the scope of the role first. Could you share "
                         "the budgeted range for the position?")
            answered.append("salary")
        for t in cls.get("candidate_tasks", []) or []:
            due = f" by {fmt_local(t['due'], self.cfg.profile.timezone).rsplit(',', 1)[0]}" if t.get("due") else ""
            lines.append(f"Thanks for sending this over - I'll take care of it{due}.")
            commitments.append(t["title"])
            break
        kind = sched.get("kind", "none")
        slots = sched.get("slots", [])
        if kind == "accept_proposed":
            lines.append(f"{sched.get('accepted_label', 'That time')} works well for me"
                         + (" - I'll send over a calendar invite." if sched.get("booking") else "."))
            commitments.append("attend the proposed call")
        elif kind in ("conflict_counter", "propose_slots") and slots:
            intro = ("Unfortunately I'm not available at that time. Would any of these work instead?"
                     if kind == "conflict_counter" else "Here are a few times that work for me:")
            lines.append(intro)
            lines.extend(f"- {s['label']}" for s in slots)
        elif kind == "invite_accept":
            lines.append("I've accepted the invite - looking forward to it.")
        if kind == "none" and "rejection" not in cats and "offer" not in cats and not cls.get("candidate_tasks"):
            lines.append("I'd love to set up a quick call to learn more. What times work for you this week?")
        lines += ["", "Best,", profile.signature or profile.name]
        body = "\n".join(lines)
        risky = cats & set(self.cfg.autonomy.hard_escalate_categories)
        confidence = 0.93
        if unanswered:
            confidence = 0.5
        elif risky:
            confidence = 0.7
        elif kind == "conflict_counter":
            confidence = 0.88
        subject = ev.get("subject") or ""
        if subject and not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"
        return {
            "reply_subject": subject,
            "reply_body": body,
            "answered_questions": answered,
            "unanswered_questions": unanswered,
            "commitments": commitments,
            "next_step": "schedule a call" if kind != "none" else "learn about next steps",
            "confidence": confidence,
            "rationale": "Offline template draft grounded in CV evidence and calendar availability.",
        }

    # -- instruct ---------------------------------------------------------
    def _instruct(self, ctx: dict[str, Any]) -> dict[str, Any]:
        note = ctx.get("instruction", "")
        low = note.lower()
        source = (ctx.get("event") or {}).get("source", "")
        times = self._parse_times(note, ctx)
        if source == "calendar" and re.search(r"\bdecline\b", low):
            meeting = "decline_invite"
        elif source == "calendar" and re.search(r"\baccept\b", low):
            meeting = "accept_invite"
        elif times:
            meeting = "book" if re.search(r"\b(book|schedule|confirm|set up)\b", low) else "propose_times"
        elif re.search(r"\b(propose|suggest|offer)\b.*\btimes?\b", low):
            meeting = "find_times"
        elif DECLINE_RE.search(note):
            meeting = "none"
        else:
            meeting = "keep"
        tasks = []
        m = re.search(r"(?:task|remind me) to (.+?)(?:\.|$)", note, re.I)
        if m:
            tasks.append({"title": m.group(1).strip().capitalize(), "kind": "other",
                          "due": self._parse_due(note, ctx), "link": "", "details": note})
        stage = "withdrawn" if re.search(r"\bwithdraw", low) else "none"
        no_reply = bool(re.search(r"\b(don't reply|do not reply|no reply|ignore it|archive it)\b", low))
        return {"reply_needed": not no_reply, "reply_guidance": note, "meeting_action": meeting, "times": times,
                "tasks": tasks, "stage": stage,
                "summary": "I'll rewrite the reply following your note" + (" and update the meeting" if meeting not in (
                    "keep", "none") else "") + "."}

    # -- critique -------------------------------------------------------
    def _critique(self, ctx: dict[str, Any]) -> dict[str, Any]:
        draft = ctx.get("draft", {})
        cls = ctx.get("classification", {})
        issues, risks = [], []
        score = 0.86
        if draft.get("unanswered_questions"):
            score = 0.55
            issues.append("Some questions are not answered from evidence.")
        cats = set(cls.get("categories", []))
        if cats & set(self.cfg.autonomy.hard_escalate_categories):
            risks.append("Touches a sensitive topic.")
            score = min(score, 0.65)
        if "rejection" in cats:
            score = 0.6
        verdict = "send" if not issues and not risks else "escalate"
        return {
            "job_advancement_score": score,
            "confidence": draft.get("confidence", 0.8),
            "verdict": verdict,
            "issues": issues,
            "risks": risks,
        }

    # -- distill --------------------------------------------------------
    def _distill(self, ctx: dict[str, Any]) -> dict[str, Any]:
        pairs = ctx.get("pairs", [])
        if not pairs:
            return {"worth_saving": False, "preference": "", "categories": [], "confidence": 0.0}
        shorter = [len(p["final"]) < 0.8 * len(p["original"]) for p in pairs if p.get("original")]
        cats = sorted({c for p in pairs for c in p.get("categories", [])})
        if shorter and all(shorter):
            avg = sum(len(p["final"].split()) for p in pairs) // max(len(pairs), 1)
            return {
                "worth_saving": True,
                "preference": f"Keep replies short and direct (about {max(avg, 40)} words or fewer).",
                "categories": cats,
                "confidence": 0.8,
            }
        added: set[str] | None = None
        for p in pairs:
            orig_lines = {l.strip() for l in p.get("original", "").splitlines() if l.strip()}
            new_lines = {l.strip() for l in p.get("final", "").splitlines() if l.strip()} - orig_lines
            added = new_lines if added is None else added & new_lines
        if added:
            line = sorted(added, key=len, reverse=True)[0]
            return {
                "worth_saving": True,
                "preference": f'Always include a line like: "{line}"',
                "categories": cats,
                "confidence": 0.75,
            }
        return {"worth_saving": False, "preference": "", "categories": cats, "confidence": 0.3}
