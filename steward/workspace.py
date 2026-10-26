"""Workspace creation and the sandbox (a fake inbox, LinkedIn, and calendar).

The sandbox lets you exercise the whole walk-away / briefing / batch loop with
no credentials: sent emails land in data/sandbox/outbox.json, bookings in
data/sandbox/calendar.json, LinkedIn replies in data/sandbox/linkedin.json.
"""

from __future__ import annotations

import os
import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from steward.config import Config, dump_config
from steward.db import Database
from steward.servers.common import JsonStore
from steward.timeutil import now_iso, to_iso, tz

PACKAGE_DATA = Path(__file__).resolve().parent / "sandbox_data"

CV_TEMPLATE = """# Your Name

you@example.com · linkedin.com/in/your-profile · City, Country

## Summary

Two or three sentences about who you are professionally and what you are looking for.

## Experience

### Job Title — Company
*Month YYYY – Present*

- One bullet per achievement. Mention the technologies you used by name (e.g. Python, Kubernetes, AWS):
  Job Sourcer computes your years of experience per technology from these role dates.

### Previous Title — Previous Company
*Month YYYY – Month YYYY*

- ...

## Skills

- **Languages:** Python (5 years), SQL
- **Infrastructure:** Docker, AWS

## Education

Degree — University, YYYY
"""

ENV_TEMPLATE = """# Secrets for Job Sourcer. Never commit this file.

# Claude (the only paid service). Alternatively run `ant auth login`.
ANTHROPIC_API_KEY=

# Or Grok, if llm.provider is "grok"
XAI_API_KEY=

# Email (IMAP/SMTP). For Gmail: enable 2-Step Verification, then create an App Password.
STEWARD_MAIL_PASSWORD=

# Notion internal integration token (optional; pipeline mirror)
NOTION_TOKEN=

# Telegram bot token (optional; notifications)
TELEGRAM_BOT_TOKEN=
"""


def init_workspace(target: Path, sandbox: bool, force: bool = False, cv_source: Path | None = None) -> Path:
    target = target.resolve()
    target.mkdir(parents=True, exist_ok=True)
    config_path = target / "steward.yaml"
    if config_path.exists() and not force:
        raise FileExistsError(f"{config_path} already exists (use --force to overwrite)")
    for sub in ("cv", "data", "secrets"):
        (target / sub).mkdir(exist_ok=True)

    cfg = Config()
    cfg.config_path = str(config_path)
    if sandbox:
        cfg.profile.name = "Alex Rivera"
        cfg.profile.email = "alex.rivera@example.com"
        cfg.profile.timezone = "America/New_York"
        cfg.profile.headline = "Senior Backend / Platform Engineer"
        cfg.profile.target_roles = ["Senior Platform Engineer", "Staff Backend Engineer"]
        cfg.profile.preferred_locations = ["Remote (US)", "New York, NY"]
        cfg.profile.work_authorization = "US citizen; no sponsorship needed"
        cfg.profile.notice_period = "I can start about four weeks after accepting an offer."
        cfg.profile.signature = "Alex"
        cfg.profile.facts = ["Open to remote or hybrid roles on the US East Coast."]
        cfg.mail.backend = "sandbox"
        cfg.calendar.backend = "sandbox"
        cfg.linkedin.backend = "sandbox"
        cfg.linkedin.business_hours = "00:00-23:59"
        cfg.linkedin.min_delay_seconds = 0
        cfg.linkedin.max_delay_seconds = 0
        cfg.whatsapp.backend = "sandbox"
        cfg.whatsapp.business_hours = "00:00-23:59"
        cfg.whatsapp.min_delay_seconds = 0
        cfg.whatsapp.max_delay_seconds = 0
        cfg.llm.provider = "anthropic" if os.environ.get("ANTHROPIC_API_KEY") else "offline"
        cfg.dashboard.port = 8766  # so the demo never collides with your real Job Sourcer on 8765
        shutil.copy(PACKAGE_DATA / "cv.md", target / "cv" / "cv.md")
    else:
        cfg.mail.backend = "imap"
        cfg.calendar.backend = "google"
        cfg.linkedin.backend = "disabled"
        cv_file = target / "cv" / "cv.md"
        if cv_source:
            shutil.copy(cv_source, cv_file)
            prefill_profile(cfg, cv_file)
        elif not cv_file.exists():
            cv_file.write_text(CV_TEMPLATE, encoding="utf-8")
    config_path.write_text(
        "# Job Sourcer configuration. See README.md for every option.\n" + dump_config(cfg), encoding="utf-8"
    )
    env_file = target / ".env"
    if not env_file.exists():
        env_file.write_text(ENV_TEMPLATE, encoding="utf-8")
    gitignore = target / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(".env\nsecrets/\ndata/\n", encoding="utf-8")
    db = Database(cfg.db_path)
    if sandbox:
        seed_sandbox(cfg, db)
    db.close()
    return config_path


def prefill_profile(cfg: Config, cv_file: Path) -> None:
    """Take your name, email, and headline from the CV so there is less to type."""
    from steward.cvparse import CV, _strip_md

    cv = CV.load(cv_file)
    if cv.name:
        cfg.profile.name = cv.name
        cfg.profile.signature = cv.name.split()[0]
        cfg.mail.from_name = cv.name
    text = cv_file.read_text(encoding="utf-8")
    email = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)
    if email:
        cfg.profile.email = email.group(0)
        cfg.mail.username = email.group(0)
    for line in text.splitlines()[1:8]:
        bold = re.match(r"\s*\*\*(.+?)\*\*", line)
        if bold:
            cfg.profile.headline = _strip_md(bold.group(1))
            cfg.profile.target_roles = [r.strip() for r in cfg.profile.headline.split("|") if r.strip()]
            break
    linkedin = re.search(r"(?:https?://)?(?:www\.)?linkedin\.com/in/[\w-]+", text)
    if linkedin:
        cfg.profile.linkedin_url = linkedin.group(0)


# -- sandbox content ------------------------------------------------------------
def _next_weekday(base: datetime, weekday: int, min_days: int = 2) -> datetime:
    d = base + timedelta(days=min_days)
    while d.weekday() != weekday:
        d += timedelta(days=1)
    return d


def _at(day: datetime, hour: int, minute: int = 0) -> datetime:
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _long_date(d: datetime) -> str:
    return f"{d.strftime('%A')}, {d.strftime('%B')} {d.day} at {d.strftime('%I:%M %p').lstrip('0')}"


def seed_sandbox(cfg: Config, db: Database, now: datetime | None = None) -> None:
    zone = tz(cfg.profile.timezone)
    now_local = (now or datetime.now(zone)).astimezone(zone)
    base = cfg.sandbox_path
    base.mkdir(parents=True, exist_ok=True)

    thursday = _next_weekday(now_local, 3)
    tuesday = _next_weekday(now_local, 1)
    proposed = _at(thursday, 14, 0)
    assessment_due = now_local + timedelta(days=4)

    def ago(hours: float) -> str:
        return to_iso(now_local - timedelta(hours=hours))

    messages = [
        {
            "uid": 101, "message_id": "<np-001@northwind-payments.example>",
            "from_name": "Priya Shah", "from_email": "priya.shah@northwind-payments.example",
            "to": [cfg.profile.email], "cc": [], "date": ago(5), "in_reply_to": "", "references": [],
            "subject": "Senior Platform Engineer at Northwind Payments",
            "body_text": (
                "Hi Alex,\n\nI'm Priya, a technical recruiter at Northwind Payments. We're hiring a Senior Platform "
                "Engineer to lead our move to Kubernetes.\n\nDo you have production experience with Kubernetes? "
                "And how many years have you worked with Go?\n\nIf it sounds interesting, I'd love to tell you "
                "more.\n\nBest,\nPriya"
            ),
        },
        {
            "uid": 102, "message_id": "<la-311@lumen-analytics.example>",
            "from_name": "Marcus Lee", "from_email": "marcus.lee@lumen-analytics.example",
            "to": [cfg.profile.email], "cc": [], "date": ago(4), "in_reply_to": "", "references": [],
            "subject": "Intro call - Staff Backend Engineer",
            "body_text": (
                "Hi Alex,\n\nThanks for connecting! I'm a recruiter at Lumen Analytics and I'd love to set up a "
                "30-minute intro call about our Staff Backend Engineer role. Would "
                f"{_long_date(proposed)} work for you?\n\nThanks,\nMarcus"
            ),
        },
        {
            "uid": 103, "message_id": "<bp-552@brightpath-health.example>",
            "from_name": "Dana Kim", "from_email": "dana.kim@brightpath-health.example",
            "to": [cfg.profile.email], "cc": [], "date": ago(3), "in_reply_to": "<bp-551@brightpath-health.example>",
            "references": ["<bp-551@brightpath-health.example>"],
            "subject": "Re: Engineering Manager, Platform",
            "body_text": (
                "Hi Alex,\n\nGreat speaking yesterday! Before we move you to the technical round at Brightpath "
                "Health, what are your salary expectations for this role?\n\nThanks,\nDana"
            ),
        },
        {
            "uid": 104, "message_id": "<ol-900@orbital-labs.example>",
            "from_name": "Riley Chen", "from_email": "riley.chen@orbital-labs.example",
            "to": [cfg.profile.email], "cc": [], "date": ago(2.5), "in_reply_to": "", "references": [],
            "subject": "Offer - Senior Software Engineer",
            "body_text": (
                "Hi Alex,\n\nI'm thrilled to let you know that we'd like to extend you an offer for the Senior "
                "Software Engineer position at Orbital Labs! The formal offer letter will follow.\n\nCould you let me "
                "know your preferred start date?\n\nCongratulations,\nRiley"
            ),
        },
        {
            "uid": 105, "message_id": "<dw-77@devweekly.example>",
            "from_name": "Dev Weekly", "from_email": "digest@devweekly.example",
            "to": [cfg.profile.email], "cc": [], "date": ago(2), "in_reply_to": "", "references": [],
            "subject": "Your weekly engineering digest",
            "body_text": (
                "This week: ten tips for faster CI pipelines, a deep dive into Postgres indexes, and more.\n\n"
                "Unsubscribe from this newsletter."
            ),
        },
        {
            "uid": 106, "message_id": "<qs-404@quanta-systems.example>",
            "from_name": "Elena Petrova", "from_email": "elena.petrova@quanta-systems.example",
            "to": [cfg.profile.email], "cc": [], "date": ago(1.5), "in_reply_to": "", "references": [],
            "subject": "Hiring manager chat - Backend Engineer (Python)",
            "body_text": (
                "Hi Alex,\n\nThe hiring manager for our Backend Engineer role at Quanta Systems would like to chat "
                "next week. When are you free for a 30-minute call?\n\nBest regards,\nElena"
            ),
        },
        {
            "uid": 107, "message_id": "<nd-220@nimbus-data.example>",
            "from_name": "Hannah Brooks", "from_email": "hannah.brooks@nimbus-data.example",
            "to": [cfg.profile.email], "cc": [], "date": ago(1), "in_reply_to": "", "references": [],
            "subject": "Next step: online assessment - Senior Data Engineer",
            "body_text": (
                "Hi Alex,\n\nThanks for your interest in the Senior Data Engineer role at Nimbus Data! The next step "
                "is a 60-minute online assessment on Codility: https://app.codility.com/test/nimbus-demo\n\n"
                f"Please complete it by {_long_date(assessment_due).split(' at ')[0]}. Let me know if you have any "
                "questions.\n\nBest,\nHannah"
            ),
        },
    ]
    JsonStore(base / "mailbox.json", {}).save({"messages": messages})
    JsonStore(base / "outbox.json", {}).save({"sent": []})

    conversations = [
        {
            "id": "li-201",
            "participant": {"name": "Sam Ortiz", "headline": "Talent Partner at Helix Robotics",
                            "profile_url": "https://www.linkedin.com/in/sam-ortiz-example"},
            "messages": [{"id": "m-1", "from": "them", "sent_at": ago(6), "text": (
                "Hi Alex! Are you open to new opportunities? We're hiring a Staff Engineer at Helix Robotics. "
                "Do you have experience with Rust?")}],
        },
        {
            "id": "li-202",
            "participant": {"name": "Jordan Blake", "headline": "Technical Recruiter at Cedar Fintech",
                            "profile_url": "https://www.linkedin.com/in/jordan-blake-example"},
            "messages": [{"id": "m-2", "from": "them", "sent_at": ago(3.5), "text": (
                "Hi Alex, I came across your profile - we're hiring a Senior Backend Engineer at Cedar Fintech "
                "working mostly with Python and AWS. Would you be open to a quick chat?")}],
        },
    ]
    JsonStore(base / "linkedin.json", {}).save({"conversations": conversations, "sent": []})

    # WhatsApp: one recruiter, and chats that are not about a job (ignored completely).
    chats = [
        {"id": "5215512345678@s.whatsapp.net", "name": "Mariana López", "messages": [
            {"id": "wa-1", "from": "them", "sent_at": ago(2.5), "text": (
                "Hola Alex, soy Mariana, reclutadora en Aurora Logistics. Tenemos una vacante de Senior Backend "
                "Engineer (Python y AWS), 100% remoto. ¿Te interesaría una llamada de 20 minutos esta semana?")}]},
        {"id": "5215598765432@s.whatsapp.net", "name": "Mamá", "messages": [
            {"id": "wa-2", "from": "them", "sent_at": ago(1.5), "text": "¿Vienes a comer el domingo? Hice mole."}]},
        {"id": "5215511122233@s.whatsapp.net", "name": "Diego", "messages": [
            {"id": "wa-3", "from": "them", "sent_at": ago(1), "text": (
                "Oye, salgo del trabajo a las 7, ¿nos vemos para el partido?")}]},
        {"id": "120363000000000001@g.us", "name": "Familia", "messages": [
            {"id": "wa-4", "from": "them", "sent_at": ago(0.5), "text": (
                "Hay una vacante en la empresa de mi primo, ¿a alguien le interesa?")}]},
    ]
    JsonStore(base / "whatsapp.json", {}).save({"chats": chats, "sent": []})

    events: list[dict[str, Any]] = []
    day = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    for i in range(0, 22):
        d = day + timedelta(days=i)
        if d.weekday() < 5:
            events.append({
                "id": f"sync-{i}", "title": "Team sync", "start": to_iso(_at(d, 14, 30)), "end": to_iso(_at(d, 15, 0)),
                "attendees": [], "organizer": {"email": cfg.profile.email, "name": cfg.profile.name},
                "my_response": "accepted", "meeting_link": "https://meet.example.com/team-sync",
            })
        if d.weekday() == 2:
            events.append({
                "id": f"focus-{i}", "title": "Focus block", "start": to_iso(_at(d, 10, 0)), "end": to_iso(_at(d, 12, 0)),
                "attendees": [], "organizer": {"email": cfg.profile.email, "name": cfg.profile.name},
                "my_response": "accepted",
            })
    interview = _at(tuesday, 11, 0)
    events.append({
        "id": "cobalt-tech-1", "title": "Cobalt Cloud - Technical Interview",
        "start": to_iso(interview), "end": to_iso(interview + timedelta(hours=1)),
        "description": "Technical interview for the Senior Platform Engineer role at Cobalt Cloud. "
                       "System design and a Kubernetes deep dive.",
        "meeting_link": "https://meet.example.com/cobalt-tech",
        "attendees": [{"email": cfg.profile.email, "name": cfg.profile.name, "response": "needsAction"},
                      {"email": "nina.ross@cobalt-cloud.example", "name": "Nina Ross", "response": "accepted"}],
        "organizer": {"email": "nina.ross@cobalt-cloud.example", "name": "Nina Ross"},
        "my_response": "needsAction",
    })
    JsonStore(base / "calendar.json", {}).save({"events": events})

    stamp = now_iso()
    for company, role, stage, name, email in [
        ("Brightpath Health", "Engineering Manager, Platform", "screening", "Dana Kim",
         "dana.kim@brightpath-health.example"),
        ("Orbital Labs", "Senior Software Engineer", "final", "Riley Chen", "riley.chen@orbital-labs.example"),
    ]:
        db.insert("opportunities", {
            "company": company, "role": role, "stage": stage, "recruiter_name": name, "recruiter_email": email,
            "channel": "email", "created_at": stamp, "updated_at": stamp, "last_inbound_at": ago(30),
            "last_outbound_at": ago(26),
        })


def inject_email(cfg: Config, from_name: str, from_email: str, subject: str, body: str,
                 in_reply_to: str = "") -> int:
    store = JsonStore(cfg.sandbox_path / "mailbox.json", {"messages": []})
    data = store.load()
    uid = max([int(m["uid"]) for m in data["messages"]] + [100]) + 1
    data["messages"].append({
        "uid": uid, "message_id": f"<inj-{uid}@{from_email.split('@')[-1]}>", "from_name": from_name,
        "from_email": from_email, "to": [cfg.profile.email], "cc": [], "date": now_iso(),
        "in_reply_to": in_reply_to, "references": [in_reply_to] if in_reply_to else [],
        "subject": subject, "body_text": body,
    })
    store.save(data)
    return uid


def inject_whatsapp(cfg: Config, name: str, text: str, chat_id: str = "") -> str:
    store = JsonStore(cfg.sandbox_path / "whatsapp.json", {"chats": [], "sent": []})
    data = store.load()
    chat = next((c for c in data["chats"] if c["id"] == chat_id), None) if chat_id else None
    if chat is None:
        chat_id = chat_id or f"52155{len(data['chats']):08d}@s.whatsapp.net"
        chat = {"id": chat_id, "name": name, "messages": []}
        data["chats"].append(chat)
    chat["messages"].append({"id": f"wa-{len(chat['messages']) + 1}-{chat_id.split('@')[0]}", "from": "them",
                             "sent_at": now_iso(), "text": text})
    store.save(data)
    return chat_id


def inject_linkedin(cfg: Config, name: str, headline: str, text: str, conversation_id: str = "") -> str:
    store = JsonStore(cfg.sandbox_path / "linkedin.json", {"conversations": [], "sent": []})
    data = store.load()
    conv = next((c for c in data["conversations"] if c["id"] == conversation_id), None) if conversation_id else None
    if conv is None:
        conversation_id = conversation_id or f"li-{300 + len(data['conversations'])}"
        conv = {"id": conversation_id, "participant": {"name": name, "headline": headline, "profile_url": ""},
                "messages": []}
        data["conversations"].append(conv)
    conv["messages"].append({"id": f"m-{len(conv['messages']) + 1}-{conversation_id}", "from": "them",
                             "sent_at": now_iso(), "text": text})
    store.save(data)
    return conversation_id
