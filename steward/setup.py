"""Setup from the dashboard: what is configured, what is missing, and how to fix it.

Everything here edits files in your own workspace: steward.yaml (settings) and
.env (secrets). Secret values are written once and never sent back to the page.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
from pathlib import Path
from typing import Any

import yaml

from steward.config import Config, dump_config, load_config
from steward.cvparse import CV

SECRETS = {
    "ANTHROPIC_API_KEY": "Claude API key",
    "XAI_API_KEY": "Grok API key (only if you use Grok)",
    "STEWARD_MAIL_PASSWORD": "Email app password",
    "NOTION_TOKEN": "Notion integration secret",
    "TELEGRAM_BOT_TOKEN": "Telegram bot token (optional notifications)",
    "LINKEDIN_TOOLKIT_TOKEN": "LinkedIn toolkit pairing token",
}

# Settings the dashboard may change. Anything else stays a deliberate edit of steward.yaml.
EDITABLE_PREFIXES = ("profile.", "mail.", "calendar.", "scheduling.", "linkedin.backend", "linkedin.upstream",
                     "linkedin.hourly_cap",
                     "linkedin.daily_cap", "linkedin.business_hours", "notion.", "notifications.",
                     "autonomy.execute_on_approve", "autonomy.follow_up_after_days", "llm.provider", "llm.model",
                     "llm.claude_code_model")
STARTUP_NAME = "Job Sourcer.cmd"


# -- config and secrets ----------------------------------------------------------
def update_config(cfg: Config, changes: dict[str, Any]) -> Config:
    path = Path(cfg.config_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    for dotted, value in changes.items():
        if not dotted.startswith(EDITABLE_PREFIXES):
            raise ValueError(f"{dotted} cannot be changed from the dashboard")
        node = raw
        keys = dotted.split(".")
        for k in keys[:-1]:
            node = node.setdefault(k, {})
        node[keys[-1]] = value
    new = Config.model_validate(raw)  # raises on invalid values; nothing is written
    new.config_path = cfg.config_path
    header = "# Job Sourcer configuration. See README.md and SETUP.md for every option.\n"
    path.write_text(header + dump_config(new), encoding="utf-8")
    return load_config(path)


def env_file(cfg: Config) -> Path:
    return cfg.base_dir / ".env"


def set_secret(cfg: Config, name: str, value: str) -> None:
    if name not in SECRETS and name != cfg.mail.password_env and name != cfg.notion.token_env:
        raise ValueError(f"{name} is not a secret Job Sourcer manages")
    value = value.strip()
    if "\n" in value or "\r" in value:
        raise ValueError("Secrets must be a single line")
    path = env_file(cfg)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    out, done = [], False
    for line in lines:
        if re.match(rf"^\s*{re.escape(name)}\s*=", line):
            out.append(f"{name}={value}")
            done = True
        else:
            out.append(line)
    if not done:
        out.append(f"{name}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    if value:
        os.environ[name] = value
    else:
        os.environ.pop(name, None)


def secret_status(cfg: Config) -> dict[str, bool]:
    names = set(SECRETS) | {cfg.mail.password_env, cfg.notion.token_env}
    return {n: bool(os.environ.get(n)) for n in sorted(names)}


def notion_page_id(text: str) -> str:
    """Accept a Notion page URL or ID and return the 32-hex ID with dashes."""
    m = re.findall(r"[0-9a-fA-F]{32}", text.replace("-", ""))
    if not m:
        raise ValueError("That does not look like a Notion page link or ID")
    h = m[-1].lower()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


# -- start at login (Windows) --------------------------------------------------------
def startup_file() -> Path | None:
    appdata = os.environ.get("APPDATA")
    if sys.platform != "win32" or not appdata:
        return None
    folder = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    old = folder / "The Steward.cmd"  # the entry's name before the app was renamed
    if old.exists() and not (folder / STARTUP_NAME).exists():
        old.rename(folder / STARTUP_NAME)
    return folder / STARTUP_NAME


def set_autostart(cfg: Config, enable: bool) -> bool:
    target = startup_file()
    if target is None:
        raise RuntimeError("Start-at-login is only automated on Windows; use your OS's startup items instead")
    if not enable:
        target.unlink(missing_ok=True)
        return False
    target.write_text(
        "@echo off\r\n"
        f'"{sys.executable}" -m steward -c "{cfg.config_path}" app --no-window\r\n', encoding="utf-8")
    return True


# -- checks ---------------------------------------------------------------------
def _step(id_: str, title: str, status: str, detail: str, fix: str = "") -> dict[str, Any]:
    return {"id": id_, "title": title, "status": status, "detail": detail, "fix": fix}


def checks(cfg: Config) -> list[dict[str, Any]]:
    p = cfg.profile
    out = []
    missing = [label for label, val in (("time zone", p.timezone != "UTC"), ("notice period", p.notice_period),
                                        ("work authorization", p.work_authorization), ("target roles", p.target_roles),
                                        ("signature", p.signature)) if not val]
    out.append(_step("profile", "Your profile", "todo" if missing else "ok",
                     "Missing: " + ", ".join(missing) if missing else f"{p.name} · {p.timezone}",
                     "Fill in the Profile form below. Drafts may only state these facts about you."))

    cv = CV.load(cfg.resolve(cfg.cv.path), cfg.cv.aliases, exclude_sections=cfg.cv.exclude_sections)
    cv_ok = bool(cv.roles)
    out.append(_step("cv", "Your CV", "ok" if cv_ok else "todo",
                     f"{len(cv.roles)} roles, {len(cv.list_skills())} skills, {cv.total_years()} years of experience"
                     if cv_ok else "No roles with date ranges found",
                     f"Edit {cfg.resolve(cfg.cv.path)}. Give each role a date range like 'Mar 2022 – Present'."))

    if cfg.llm.provider == "claude-code":
        from steward.llm.claude_code_provider import auth_status

        st = auth_status(cfg.llm.claude_code_path)
        login_fix = ("Open a terminal, run `claude`, type /login and choose your Claude account (subscription), "
                     "then press Test connection.")
        if not st["installed"]:
            out.append(_step("llm", "Claude (your subscription)", "todo", "Claude Code is not installed",
                             "Install it: winget install Anthropic.ClaudeCode. " + login_fix))
        elif not st.get("loggedIn"):
            out.append(_step("llm", "Claude (your subscription)", "todo", "Claude Code is not signed in", login_fix))
        elif st.get("authMethod") != "claude.ai":
            out.append(_step("llm", "Claude (your subscription)", "warn",
                             f"Claude Code is signed in with {st.get('authMethod')}, not your Claude subscription",
                             "Run `claude`, type /logout, then /login with your Claude account."))
        else:
            out.append(_step("llm", "Claude (your subscription)", "ok",
                             f"Signed in as {st.get('email')} ({st.get('subscriptionType')} plan) · model "
                             f"{cfg.llm.claude_code_model} · no API key needed"))
    elif cfg.llm.provider == "anthropic":
        has = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
        out.append(_step("llm", "Claude", "ok" if has else "todo",
                         f"Model {cfg.llm.model}" + ("" if has else " · no API key yet"),
                         "Create a key at console.anthropic.com > API Keys and paste it below."))
    elif cfg.llm.provider == "grok":
        has = bool(os.environ.get(cfg.llm.grok_api_key_env))
        out.append(_step("llm", "Grok", "ok" if has else "todo", cfg.llm.grok_model, "Paste your xAI API key below."))
    else:
        out.append(_step("llm", "Language model", "warn", "Offline demo heuristics (not for real recruiters)",
                         "Switch the provider to Claude in the Profile & settings form."))

    m = cfg.mail
    if m.backend == "imap":
        has_pw = bool(os.environ.get(m.password_env))
        ok = bool(m.username) and has_pw
        out.append(_step("mail", "Email", "ok" if ok else "todo",
                         f"{m.username or '(no address)'} via {m.imap_host} · folder {m.folder}"
                         + ("" if has_pw else " · no app password yet"),
                         "Gmail: Google Account > Security > 2-Step Verification > App passwords. Paste it below."))
    else:
        out.append(_step("mail", "Email", "off" if m.backend == "disabled" else "warn", f"backend: {m.backend}",
                         "Set the email backend to imap for your real inbox."))

    c = cfg.calendar
    if c.backend == "google":
        libs = importlib.util.find_spec("googleapiclient") is not None
        creds = cfg.resolve(c.credentials_file).exists()
        token = cfg.resolve(c.token_file).exists()
        status = "ok" if libs and creds and token else "todo"
        detail = "connected" if status == "ok" else ", ".join(x for x, ok in (
            ("Google libraries not installed", libs), (f"no OAuth client file at {c.credentials_file}", creds),
            ("not connected yet", token)) if not ok)
        out.append(_step("calendar", "Google Calendar", status, detail,
                         "Create a Desktop OAuth client in Google Cloud, save the JSON as "
                         f"{cfg.resolve(c.credentials_file)}, then press Connect Google Calendar."))
    else:
        out.append(_step("calendar", "Calendar", "off" if c.backend == "disabled" else "warn", f"backend: {c.backend}",
                         "Set the calendar backend to google for your real calendar."))

    li = cfg.linkedin
    if li.backend == "upstream" and li.upstream is not None and li.upstream.url:
        from steward.linkedin_bridge import status as bridge_status

        token_ok = not li.upstream.token_env or bool(os.environ.get(li.upstream.token_env))
        bridge = bridge_status(cfg)
        # linkedin-toolkit 2.1.1 cannot send messages unpatched (patches/linkedin-toolkit-2.1.1).
        ext = Path.home() / ".linkedin-toolkit" / "extension" / "src" / "background" / "voyager.js"
        unpatched = ext.exists() and "Job Sourcer patch" not in ext.read_text(encoding="utf-8", errors="replace")
        problems = [p for p, bad in (("pairing token not saved", not token_ok),
                                     ("bridge not running yet (Job Sourcer starts it on its next check)",
                                      bridge.get("local") and not bridge.get("running")),
                                     ("the extension cannot send messages until you apply "
                                      "patches/linkedin-toolkit-2.1.1 (see its README)", unpatched)) if bad]
        out.append(_step("linkedin", "LinkedIn", "todo" if not token_ok else ("warn" if problems else "ok"),
                         f"linkedin-toolkit bridge at {li.upstream.url}" + (" · " + "; ".join(problems) if problems else
                                                                            " · running"),
                         "Paste the pairing token below (from: npx -y -p linkedin-toolkit-mcp lit config get token "
                         "--reveal), keep Edge (or the browser with the extension) open, then press Test connection."))
    elif li.backend == "upstream":
        ok = li.upstream is not None
        out.append(_step("linkedin", "LinkedIn", "warn" if ok else "todo",
                         f"via {li.upstream.command} {' '.join(li.upstream.args)} (started per check; the extension "
                         "may not stay connected)" if ok else "upstream not configured",
                         "Press 'Use linkedin-toolkit' to switch to the always-on bridge."))
    else:
        out.append(_step("linkedin", "LinkedIn", "off" if li.backend == "disabled" else "warn", f"backend: {li.backend}",
                         "Optional. See SETUP.md step 6 to connect LinkedIn through your own browser session."))

    n = cfg.notion
    if n.enabled:
        has = bool(os.environ.get(n.token_env))
        ok = has and bool(n.database_id)
        out.append(_step("notion", "Notion pipeline", "ok" if ok else "todo",
                         f"database {n.database_id or '(none)'}" + ("" if has else " · no token"),
                         "Paste the integration secret below, then create the database from a shared page."))
    else:
        out.append(_step("notion", "Notion pipeline", "off", "Optional. Your pipeline is always kept locally too.",
                         "Turn it on below if you want the pipeline mirrored to Notion."))

    nt = cfg.notifications
    alerts = (["sound on new events"] if nt.sound else []) + nt.channels
    out.append(_step("notifications", "Notifications", "ok" if alerts else "off",
                     ", ".join(alerts) or "Optional. The dashboard and desktop alerts always work.",
                     "Add an ntfy topic URL or a Telegram bot to get briefings on your phone."))
    sf = startup_file()
    out.append(_step("autostart", "Start when you log in", "ok" if sf and sf.exists() else "off",
                     "enabled" if sf and sf.exists() else "Job Sourcer runs only while you start it",
                     "Turn on start-at-login so Job Sourcer keeps watching without you opening anything."))
    return out


def editable_values(cfg: Config) -> dict[str, Any]:
    p, m = cfg.profile, cfg.mail
    return {
        "profile.name": p.name, "profile.email": p.email, "profile.timezone": p.timezone,
        "profile.headline": p.headline, "profile.linkedin_url": p.linkedin_url,
        "profile.target_roles": p.target_roles, "profile.preferred_locations": p.preferred_locations,
        "profile.remote_preference": p.remote_preference, "profile.work_authorization": p.work_authorization,
        "profile.notice_period": p.notice_period, "profile.relocation": p.relocation,
        "profile.salary_policy": p.salary_policy, "profile.salary_expectation_private": p.salary_expectation_private,
        "profile.signature": p.signature, "profile.facts": p.facts,
        "llm.provider": cfg.llm.provider, "llm.model": cfg.llm.model,
        "llm.claude_code_model": cfg.llm.claude_code_model,
        "mail.backend": m.backend, "mail.username": m.username, "mail.imap_host": m.imap_host,
        "mail.smtp_host": m.smtp_host, "mail.folder": m.folder,
        "calendar.backend": cfg.calendar.backend,
        "scheduling.work_start": cfg.scheduling.work_start, "scheduling.work_end": cfg.scheduling.work_end,
        "scheduling.buffer_minutes": cfg.scheduling.buffer_minutes,
        "linkedin.backend": cfg.linkedin.backend,
        "notion.enabled": cfg.notion.enabled, "notion.database_id": cfg.notion.database_id,
        "notifications.channels": cfg.notifications.channels, "notifications.ntfy_url": cfg.notifications.ntfy_url,
        "notifications.telegram_chat_id": cfg.notifications.telegram_chat_id,
        "notifications.quiet_hours": cfg.notifications.quiet_hours,
        "notifications.sound": cfg.notifications.sound,
        "autonomy.execute_on_approve": cfg.autonomy.execute_on_approve,
    }


def google_connect(cfg: Config) -> str:
    """Run Google's consent screen in your browser and save the token (blocks until you finish)."""
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise RuntimeError("Install the Google libraries first: pip install -e .[google]") from exc
    creds_file = cfg.resolve(cfg.calendar.credentials_file)
    if not creds_file.exists():
        raise RuntimeError(f"Save your Google OAuth client JSON as {creds_file} first")
    flow = InstalledAppFlow.from_client_secrets_file(str(creds_file), ["https://www.googleapis.com/auth/calendar"])
    creds = flow.run_local_server(port=0, timeout_seconds=300)
    token = cfg.resolve(cfg.calendar.token_file)
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(creds.to_json(), encoding="utf-8")
    return str(token)
