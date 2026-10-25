"""Configuration: one YAML file per workspace, secrets in a sibling .env file.

Paths inside the config are resolved relative to the directory that holds the
config file, so a workspace folder can be moved around as a unit.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

CONFIG_ENV = "STEWARD_CONFIG"
DEFAULT_CONFIG_NAME = "steward.yaml"


class ServerSpec(BaseModel):
    """An external MCP server: started over stdio (`command`) or reached over HTTP (`url`)."""

    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    # Streamable-HTTP endpoint, e.g. http://127.0.0.1:47830/mcp; takes precedence over `command`.
    url: str = ""
    # Name of the .env variable holding the bearer token for `url`.
    token_env: str = ""
    # Maps Job Sourcer's logical tool names to the external server's tool names.
    tools: dict[str, str] = Field(default_factory=dict)


class Profile(BaseModel):
    name: str = "Your Name"
    email: str = ""
    timezone: str = "UTC"
    headline: str = ""
    linkedin_url: str = ""
    target_roles: list[str] = Field(default_factory=list)
    preferred_locations: list[str] = Field(default_factory=list)
    remote_preference: str = "Remote or hybrid"
    work_authorization: str = ""
    notice_period: str = ""
    relocation: str = "Open to discussing relocation for the right role"
    salary_policy: str = (
        "Never state a number first. Defer compensation until after the technical stage; "
        "if pressed, ask for the budgeted range for the role."
    )
    # Private context for the briefing only. Drafts are instructed never to disclose it.
    salary_expectation_private: str = ""
    # Additional facts the drafter may state verbatim (e.g. "Available to start in 4 weeks").
    facts: list[str] = Field(default_factory=list)
    signature: str = ""


class LLMConfig(BaseModel):
    # claude-code: your Claude Pro/Max subscription, through the official Claude Code program (no API key).
    # anthropic: the Claude API with an API key. grok: xAI. offline: demo heuristics.
    provider: Literal["claude-code", "anthropic", "grok", "offline"] = "claude-code"
    model: str = "claude-opus-5-5"
    # Per-purpose overrides: classify, draft, critique, distill.
    models: dict[str, str] = Field(default_factory=dict)
    effort: dict[str, str] = Field(
        default_factory=lambda: {"classify": "low", "draft": "medium", "critique": "low", "distill": "low",
                                 "instruct": "low"}
    )
    refusal_fallback: bool = True
    grok_model: str = "grok-4"
    grok_api_key_env: str = "XAI_API_KEY"
    max_tokens: int = 16000
    # Claude Code settings (provider: claude-code). Model aliases: opus, sonnet, haiku, or a full model name.
    claude_code_model: str = "opus"
    claude_code_path: str = ""
    claude_code_timeout: int = 300


class MailConfig(BaseModel):
    backend: Literal["imap", "sandbox", "disabled"] = "sandbox"
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 465
    smtp_starttls: bool = False
    username: str = ""
    password_env: str = "STEWARD_MAIL_PASSWORD"
    from_name: str = ""
    folder: str = "INBOX"
    save_sent_folder: str = ""
    # Where your own sent mail is (to notice replies you wrote yourself). Empty = find it automatically.
    sent_folder: str = ""
    # "Thank you for applying" emails that ask nothing of you: marked as read and moved to this label.
    file_application_acks: bool = True
    applications_label: str = "Jobs"
    # Once an item is handled (reply sent, rejected, discarded, or answered by you), its emails are marked as
    # read and moved to applications_label, out of the inbox.
    cleanup_handled: bool = True
    initial_lookback_days: int = 7
    max_per_poll: int = 40
    prefilter: Literal["keywords", "all"] = "keywords"
    poll_minutes: int = 1
    server: ServerSpec | None = None


class CalendarConfig(BaseModel):
    backend: Literal["google", "sandbox", "disabled"] = "sandbox"
    calendar_id: str = "primary"
    credentials_file: str = "secrets/google_credentials.json"
    token_file: str = "secrets/google_token.json"
    poll_minutes: int = 2
    invite_lookahead_days: int = 21
    server: ServerSpec | None = None


class SchedulingConfig(BaseModel):
    working_days: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4])  # Monday = 0
    work_start: str = "09:00"
    work_end: str = "17:30"
    buffer_minutes: int = 15
    default_duration_minutes: int = 30
    min_notice_hours: int = 18
    lookahead_days: int = 10
    max_slots: int = 3
    avoid: list[str] = Field(default_factory=list)  # e.g. ["Fri 15:00-18:00"]
    book_on_accept: bool = True
    add_video_link: bool = True
    meeting_title: str = "{company} - {role} ({recruiter})"


class LinkedInConfig(BaseModel):
    backend: Literal["upstream", "sandbox", "disabled"] = "sandbox"
    poll_minutes: int = 10
    hourly_cap: int = 8
    daily_cap: int = 30
    business_hours: str = "09:00-19:00"
    min_delay_seconds: int = 60
    max_delay_seconds: int = 240
    # The upstream LinkedIn MCP server that drives your own logged-in browser
    # session (for example the linkedin-toolkit Edge/Chrome extension + MCP server).
    upstream: ServerSpec | None = None
    server: ServerSpec | None = None
    # When upstream.url points at this machine, Job Sourcer keeps the toolkit's long-running bridge
    # (`lit serve --http`) alive so the browser extension stays connected to it.
    manage_bridge: bool = True
    bridge_command: list[str] = Field(
        default_factory=lambda: ["npx", "-y", "-p", "linkedin-toolkit-mcp", "lit", "serve", "--http"])


class NotionConfig(BaseModel):
    enabled: bool = False
    token_env: str = "NOTION_TOKEN"
    database_id: str = ""
    server: ServerSpec | None = None


class CVConfig(BaseModel):
    path: str = "cv"
    aliases: dict[str, list[str]] = Field(default_factory=dict)
    # Section or role names (substrings) whose lines are never quoted to recruiters.
    exclude_sections: list[str] = Field(default_factory=list)
    server: ServerSpec | None = None


class AutonomyConfig(BaseModel):
    hard_escalate_categories: list[str] = Field(
        default_factory=lambda: [
            "salary_question",
            "offer",
            "negotiation",
            "references_background",
            "relocation_question",
            "work_authorization",
        ]
    )
    band_high: float = 0.9
    band_low: float = 0.6
    target_precision: float = 0.9
    calibration_min_samples: int = 20
    job_advancement_flag: float = 0.5
    job_advancement_auto_send_min: float = 0.7
    default_rule_min_confidence: float = 0.85
    propose_rule_after: int = 3
    propose_promotion_after: int = 10
    shadow_matches_required: int = 3
    decay_days: int = 60
    preference_min_edits: int = 2
    follow_up_after_days: int = 5
    max_processing_attempts: int = 3
    # Run each approval right away instead of waiting for "Go" (also switchable in the dashboard).
    execute_on_approve: bool = False


class NotificationsConfig(BaseModel):
    channels: list[Literal["ntfy", "telegram"]] = Field(default_factory=list)
    ntfy_url: str = ""
    telegram_bot_token_env: str = "TELEGRAM_BOT_TOKEN"
    telegram_chat_id: str = ""
    quiet_hours: str = "22:00-07:30"
    min_pending: int = 3
    cooldown_minutes: int = 90
    meeting_reminder_minutes: int = 15
    # Chime on this computer whenever a check brings new events (a recruiter message, call, invite, or meeting
    # change). Empty sound_file = the bundled assets/Tethys Nexus 5.mp3.
    sound: bool = True
    sound_file: str = ""


class DashboardConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765
    tick_seconds: int = 30


class Config(BaseModel):
    profile: Profile = Field(default_factory=Profile)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    mail: MailConfig = Field(default_factory=MailConfig)
    calendar: CalendarConfig = Field(default_factory=CalendarConfig)
    scheduling: SchedulingConfig = Field(default_factory=SchedulingConfig)
    linkedin: LinkedInConfig = Field(default_factory=LinkedInConfig)
    notion: NotionConfig = Field(default_factory=NotionConfig)
    cv: CVConfig = Field(default_factory=CVConfig)
    autonomy: AutonomyConfig = Field(default_factory=AutonomyConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    data_dir: str = "data"

    # Not part of the YAML: where the config was loaded from.
    config_path: str = Field(default="", exclude=True)

    @property
    def base_dir(self) -> Path:
        return Path(self.config_path).parent if self.config_path else Path.cwd()

    def resolve(self, path: str) -> Path:
        p = Path(path)
        return p if p.is_absolute() else (self.base_dir / p)

    @property
    def data_path(self) -> Path:
        return self.resolve(self.data_dir)

    @property
    def db_path(self) -> Path:
        return self.data_path / "steward.db"

    @property
    def sandbox_path(self) -> Path:
        return self.data_path / "sandbox"


def find_config_path(explicit: str | os.PathLike | None = None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    if os.environ.get(CONFIG_ENV):
        return Path(os.environ[CONFIG_ENV]).resolve()
    for candidate in (Path.cwd() / DEFAULT_CONFIG_NAME, Path.cwd() / "workspace" / DEFAULT_CONFIG_NAME):
        if candidate.exists():
            return candidate.resolve()
    return (Path.cwd() / "workspace" / DEFAULT_CONFIG_NAME).resolve()


def load_env(config_path: Path) -> None:
    env_file = config_path.parent / ".env"
    if env_file.exists():
        from dotenv import load_dotenv

        load_dotenv(env_file, override=False)


def load_config(path: str | os.PathLike | None = None) -> Config:
    config_path = find_config_path(path)
    if not config_path.exists():
        raise FileNotFoundError(
            f"No Job Sourcer config at {config_path}. Run `steward init` (or `steward init --sandbox`) first."
        )
    load_env(config_path)
    raw: dict[str, Any] = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    cfg = Config.model_validate(raw)
    cfg.config_path = str(config_path)
    return cfg


def dump_config(cfg: Config) -> str:
    data = cfg.model_dump(mode="json", exclude={"config_path"})
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
