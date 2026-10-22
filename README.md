# The Steward

**A recruiter-facing chief of staff.** The Steward is a local, self-hosted agentic harness that runs your
recruiter-facing job search asynchronously: it answers from your CV, schedules around your calendar, drafts every
reply, and escalates to you only the decisions it has not yet earned the right to make alone.

You walk away. It watches your email, LinkedIn, and calendar. When you come back, it briefs you: what it already
handled (because you taught it those are safe), what waits for your judgment, and what is high priority (salary,
offers, anything it is unsure about). You approve, edit, or reject in one sitting, then say **go**, and it executes
everything in a single batch: sends the emails, posts the LinkedIn replies, books the meetings.

Every decision is recorded in an immutable ledger. Steward notices patterns, proposes rules, and you activate them.
The briefing gets shorter. It earns autonomy one verified decision at a time.

> Not a chatbot. Not a mass-apply bot. Not a LinkedIn scraper. Not a black box. Not a replacement for you in the
> offer room: the moment a thread reaches offer stage, autonomy is revoked and everything routes to you.

The only paid component is the LLM, and by default that is **your existing Claude Pro/Max subscription** (used
through the official Claude Code program, so no API key). A Claude API key or Grok also work. Everything else is
open source and runs on your machine.

## Everyday use: just keep the window open

1. Double-click **`Start Steward.cmd`** (or turn on *Start when you log in* in Setup). Steward starts in the
   background and opens its window. There is no terminal to keep open.
2. It watches continuously: email every minute, calendar every 2 minutes, LinkedIn every 10 minutes. Every new
   event (a recruiter message, a calendar invitation, a moved or cancelled meeting, a missed call or voicemail)
   is drafted, routed, and either handled under a rule you approved or put in front of you, re-prioritized. The
   window updates by itself, with a toast, a count in the title, and optional desktop/phone notifications.
3. You read the briefing, Approve / edit / Reject / "Don't ask again", and press **Go**. To send each approval
   immediately instead, switch on **Act on each approval immediately**.
4. Not happy with a proposal? Type what to do instead in the item's **"Or tell Steward what to do instead"** field
   ("Decline politely, I only want remote roles", "Book Tuesday 10am", "Remind me to send my portfolio by Friday").
   Steward rewrites the reply and the meeting/task/stage actions and shows the result for your approval. To shape
   future replies, write a rule in **"Teach Steward a rule"** (for messages like this, this company, this recruiter,
   or everything), then *Save rule* or *Save & re-draft this*.
5. When a recruiter asks you to do something yourself (an online assessment, a form, a document, applying through
   another link), Steward suggests a **task**. Accept it and it appears in the **Tasks** tab (tick it off when done);
   deadlines show under **Upcoming meetings & deadlines** with reminders and raise the item's priority.

First-time setup with your real accounts is a page in the app (**Setup** tab) and is described step by step in
**[SETUP.md](SETUP.md)**. Your resume is already loaded into `workspace/cv/cv.md`.

---

## Quick start (sandbox, no credentials)

Double-click **`Start Steward Demo.cmd`**. Or, from a terminal:

The sandbox has a fake inbox, LinkedIn, and calendar with realistic recruiter traffic: a Kubernetes question, a
proposed time that collides with your team sync, a salary question, an offer, a newsletter, an availability
request, a LinkedIn question about a skill that is not in the CV, and a technical-interview invitation.

```bash
uv venv --python 3.12 .venv
```

```bash
uv pip install --python .venv/Scripts/python.exe -e ".[dev]"
```

```bash
.venv/Scripts/steward.exe init --sandbox --dir sandbox-demo
```

```bash
.venv/Scripts/steward.exe -c sandbox-demo/steward.yaml run --once
```

```bash
.venv/Scripts/steward.exe -c sandbox-demo/steward.yaml serve
```

Open http://127.0.0.1:8766. (On macOS/Linux use `.venv/bin/steward`.) The sandbox uses an **offline heuristic
provider** so it never spends your Claude usage; its drafts are template-quality. Set the provider to
`claude-code` in its Setup tab to see real Claude drafts on the demo data. Sent mail lands in
`sandbox-demo/data/sandbox/outbox.json`; to simulate new recruiter traffic:

```bash
.venv/Scripts/steward.exe -c sandbox-demo/steward.yaml sandbox-email --from-name "Omar Haddad" --from-email omar@vertex.example --subject "Backend role" --body "Hi! I'm a recruiter at Vertex. When are you free for a call next week?"
```

---

## How it works

```
 Watchers (MCP)            Subagents                               You            Executor (MCP)
 ───────────────           ─────────────────────────────────────    ──────────     ────────────────
 email   ─┐                Classifier  -> what is it, what's asked
 linkedin ├─> events ─>    CV Analyst  -> exact evidence from CV   ┌─ briefing ─┐
 calendar ┘   (SQLite)     Scheduler   -> free/busy, slots          │ approve    │   send email
                           Drafter     -> reply in your voice       │ edit       │─> send LinkedIn (human-paced)
                           Reviewer    -> job-advancement score     │ reject     │   book / RSVP (re-checked)
                           Router      -> ask / act+tell / silent ──┤ don't ask  │
                                 │                                  └─── go ─────┘
                                 └─ earned rule? ─> executes now, shows as "Handled automatically"

 Learner (after every batch): ledger -> mine patterns -> propose rules/preferences -> you accept -> shadow -> active
```

**Pipeline per message** (`steward/orchestrator.py`):

1. **Classifier** (LLM): categories, every question asked, technologies, proposed times, hiring-stage signal,
   suspicious-message flag.
2. **CV Analyst** (`agents/cv_analyst.py`, read-only, no LLM): calls the CV MCP server for each question. Years of
   experience come from the role date ranges in your CV (or what you state in your Skills section). If the CV has no
   evidence, the answer is "not found" and the message is escalated: the system never guesses.
3. **Scheduler** (`agents/scheduler.py`): checks proposed times against your calendar (working hours, buffers,
   minimum notice, blocked windows), finds alternatives, plans a booking or RSVP.
4. **Drafter** (LLM): writes the reply in your voice. Its objective is fixed: move you toward a signed offer (secure
   the next interview, show fit, remove scheduling friction, never leave a recruiter without a next step). It may
   only use CV evidence, the schedule, and your profile; it follows your standing preferences and imitates your most
   similar past decisions (dynamic few-shot from the ledger, including your edits).
5. **Reviewer** (LLM): scores job advancement and confidence, and escalates unsupported claims.
6. **Decision Router** (`agents/router.py`, deterministic): reads the pipeline (Notion via MCP if enabled), your
   rules, and calibration learned from your ledger. LLM scores can lower autonomy, never raise it.

**Hard guardrails** (always route to you, in this order): offer-stage lockout, sensitive categories (salary,
offer, negotiation, relocation, work authorization, references/background checks), missing CV evidence,
suspicious messages, low confidence, low job-advancement score, reviewer escalation, calendar conflicts.

**Safety invariants enforced in code**

- The LLM never acts. Outward-facing MCP tools (`send_email`, `send_message`, `create_event`, `rsvp_event`) can only
  be called with the executor's capability token (`steward/mcp_hub.py`). Subagents never get it.
- Exactly-once execution: deterministic idempotency keys per action; an interrupted attempt is marked `unknown` and
  never blindly retried (`steward/executor.py`).
- The calendar is checked when drafting and re-checked right before booking; if the slot was taken in between, the
  reply is held back and the message is re-drafted with fresh availability.
- LinkedIn pacing is non-negotiable: hourly/daily caps, business hours, and a jittered gap between sends. Sends
  that are not allowed yet are deferred and retried by the background loop.
- The decision ledger is append-only (SQLite triggers block UPDATE/DELETE) and hash-chained; `steward ledger --verify`
  recomputes the chain.
- Recruiter messages are treated as untrusted data in every prompt.
- The dashboard binds to 127.0.0.1, checks the Host header, and requires a per-process token on every write, so
  other websites cannot trigger "go".

### The autonomy ladder

| Tier | Name | How it is reached | Behavior |
| --- | --- | --- | --- |
| 0 | Ask me | default for everything | always in the briefing |
| 1 | Act and tell me | you click "Don't ask again", or accept a learner proposal (after a shadow period) | executes immediately, listed under "Handled automatically"; you can override |
| 2 | Act silently | only you promote a rule; the learner can only propose it | executes immediately, ledger only |

- **"Don't ask again"** creates a rule from a draft: same kind of message, optionally limited to technologies, a
  sender/domain, a company, or a channel, with a minimum confidence. Sensitive categories cannot be delegated.
- **Learner** (`agents/learner.py`) after each batch: proposes a rule after 3 clean approvals of the same kind;
  proposes promotion to silent after 10 executions with no override; distills a standing preference when you keep
  editing drafts the same way; recalibrates the HIGH confidence band on your own ledger once there are 20 decisions.
- **Shadow mode**: an accepted learner rule first only watches. It goes live after it agrees with your next 3
  decisions (a disagreement resets the count).
- **Overrides** ("This was wrong") demote the rule one tier immediately and feed your correction back as training
  data. **Decay**: rules unused for 60 days go dormant.

---

## Real setup

The guided version is **[SETUP.md](SETUP.md)**, done from the dashboard's Setup tab. Reference details follow.

```bash
.venv/Scripts/steward.exe init --dir workspace --cv "Bernardo/Resume En.md"
```

This creates `workspace/steward.yaml` (name, email, and headline pre-filled from the CV), `workspace/.env`, and
`workspace/cv/cv.md`. (Already done for you.) Commands find `workspace/steward.yaml` automatically when run from
the project folder; otherwise pass `-c path/to/steward.yaml`.

1. **CV**: replace `workspace/cv/cv.md` with your CV in markdown. Give every role a date range
   (`*March 2021 – Present*` under the role heading) and name the technologies you used in its bullets: years per
   technology are computed from those dates. A Skills section with `Python (9 years)` overrides the computed number.
2. **Profile** (`profile:` in steward.yaml): name, email, **timezone** (IANA, e.g. `America/Mexico_City`),
   target roles, locations, work authorization, notice period, relocation stance, salary policy, signature. These are
   the only non-CV facts drafts may state. `salary_expectation_private` is never disclosed.
3. **Claude** (`llm:`):
   - **`provider: claude-code` (default): your Claude Pro/Max subscription.** Steward runs the official, unmodified
     Claude Code program (`claude -p`) with your own sign-in, so no API key is needed and usage counts toward your
     plan. Each call replaces Claude Code's system prompt with Steward's, has no tools, loads none of your Claude
     Code settings/hooks/MCP servers, saves no session history, and returns JSON validated by `--json-schema`
     (`steward/llm/claude_code_provider.py`). Steward never touches your credentials. Model: `claude_code_model`
     (`opus`, `sonnet`, `haiku`); per-step overrides in `llm.models`. When the plan's usage limit is reached, drafting
     pauses until the reset (shown in the dashboard) and queued messages are processed afterwards.
   - `provider: anthropic`: the Claude API with `ANTHROPIC_API_KEY` (or `ant auth login`). Default model
     `claude-opus-5-5`, low effort for triage/review and medium for drafting, a cached system prompt, and the
     server-side refusal fallback (`llm.refusal_fallback`).
   - `provider: grok` with `XAI_API_KEY`.
4. **Email** (`mail:`): IMAP/SMTP, works with any provider. For Gmail: turn on 2-Step Verification, create an App
   Password, put it in `.env` as `STEWARD_MAIL_PASSWORD`, set `mail.username`. Tip: create a Gmail filter that labels
   recruiter mail `Recruiters` and set `mail.folder: Recruiters`; otherwise Steward reads INBOX and a keyword
   pre-filter keeps unrelated mail away from the LLM (`mail.prefilter`). Messages are never marked as read.
5. **Calendar** (`calendar:`): Google Calendar API (free). In Google Cloud Console create a project, enable the
   Calendar API, create an OAuth client of type *Desktop app*, save the JSON as `workspace/secrets/google_credentials.json`, then:

   ```bash
   uv pip install --python .venv/Scripts/python.exe -e ".[google]"
   ```

   ```bash
   .venv/Scripts/steward.exe google-auth
   ```

   Scheduling rules live under `scheduling:` (working days/hours, buffer, minimum notice, blocked windows such as
   `"Fri 15:00-18:00"`, whether to book automatically when a proposed time is free, Meet links).
6. **LinkedIn** (`linkedin:`): Steward talks to LinkedIn only through an upstream MCP server that drives **your own
   logged-in Chrome session** (for example the open-source linkedin-toolkit Chrome extension + MCP server). Install
   it per its instructions, then:

   ```yaml
   linkedin:
     backend: upstream
     upstream:
       command: npx            # whatever the toolkit's docs say to run
       args: ["-y", "<its MCP server package>"]
       tools: {list_conversations: list_conversations, get_conversation: get_conversation, send_message: send_message}
   ```

   Map the three logical tools to the toolkit's actual tool names. Responses are normalized best-effort; check with
   `steward doctor`. Caps: `hourly_cap`, `daily_cap`, `business_hours`, `min/max_delay_seconds`. LinkedIn automation
   always carries some account risk; keep the caps low.
7. **Notion** (optional pipeline mirror, `notion:`): create an internal integration at notion.so/my-integrations,
   put the token in `.env` as `NOTION_TOKEN`, share a page with the integration, then
   `steward notion-init --parent-page <page id>` and set `notion.enabled: true` and `notion.database_id`. If you
   move an opportunity to "Offer" in Notion, the router sees it and revokes autonomy for that thread.
8. **Notifications** (optional, `notifications:`): `ntfy` (self-hostable push to your phone) and/or Telegram; quiet
   hours, a minimum number of pending decisions, cooldown, and reminders before job meetings.
9. Check everything:

   ```bash
   .venv/Scripts/steward.exe doctor
   ```

**Any integration can be swapped** for a different MCP server: set `<section>.server: {command, args, env, tools}`
where `tools` maps Steward's tool names to the other server's.

### Running it in the background (Windows)

`Start Steward.cmd` runs `steward app`: it starts `steward serve` (dashboard + watch loop) as a hidden background
process if it is not already running, then opens the window. Logs go to `workspace/data/logs/server.log`.
Start-at-login (Setup tab, or `steward autostart --enable`) puts a small script in your Windows Startup folder that
runs `steward app --no-window`. Without the dashboard: `steward run` (same loop, in a terminal).

---

## Daily use

| You want to | Dashboard | CLI |
| --- | --- | --- |
| See the briefing | Briefing tab | `steward brief` (`--full`) |
| Decide everything in one sitting | buttons on each card | `steward review` |
| Approve / edit / reject one | Approve, edit the text then Approve, Reject | `steward approve 12`, `steward edit 12`, `steward reject 12 --note "..."` |
| Stop being asked about this kind of thing | Don't ask again... | `steward dont-ask 12 [--tech Kubernetes] [--silent]` |
| Execute all decisions | Go | `steward go` |
| Correct something done automatically | This was wrong... | `steward override 15 --note "..." --corrected "..."` |
| Have it do something else instead | "Or tell Steward what to do instead" field | `steward instruct 12 "Decline politely"` |
| Teach a rule for future replies | "Teach Steward a rule" field | `steward teach 12 "Ask for the end client" --scope company --redraft` |
| Track things you must do | Tasks tab, task suggestions on items | `steward tasks`, `--add "..." --due 2026-10-30T17:00`, `--done 3`, `--accept 4` |
| Accept/dismiss suggested automation | Accept / Dismiss | `steward accept 3`, `steward dismiss 3` |
| See or change rules | Rules & learning tab | `steward rules`, `steward rules retire 2` |
| Standing preferences | Rules & learning tab | `steward prefs --add "Always add a 15 minute buffer before calls"` |
| Pipeline | Pipeline tab | `steward pipeline`, `steward pipeline --id 4 --set-stage offer`, `--csv out.csv` |
| Audit | Ledger tab | `steward ledger`, `steward ledger --verify` |

Your only regular tasks: approve recommended actions, and join the meetings it books (they show up under
"Upcoming meetings" with join links, plus a reminder if notifications are on). It also drafts follow-ups when a
recruiter goes quiet for `autonomy.follow_up_after_days` (5) days.

---

## FAQ

**Is Notion real?** Yes. `steward/servers/notion.py` calls the real Notion API (api.notion.com) with your own
integration secret. It is **optional and off by default**: your pipeline is always stored locally in SQLite (the
Pipeline tab); Notion is a mirror you can also edit. It needs a Notion internal-integration secret
(`NOTION_TOKEN`, pasted in Setup) and a page shared with that integration (SETUP.md step 7).

**Do I need to install MCP servers?** No, with one exception. Steward ships five MCP servers (`steward/servers/`:
mail, calendar, cv, linkedin, notion) and starts each one automatically, as its own subprocess with only its own
credentials, whenever it needs it. The exception is LinkedIn: Steward's LinkedIn server is a rate-limited proxy in
front of the open-source **linkedin-toolkit** (Chrome extension + local server, run with `npx linkedin-toolkit-mcp`),
because the only safe way to use LinkedIn is through your own logged-in browser. Steward discovers the toolkit's
tool names by itself (`steward/linkedin_adapt.py`); `linkedin.upstream.tools` in steward.yaml can override them.

**Is it constantly watching?** Yes, whenever Steward is running (`Start Steward.cmd`, start-at-login, or `steward
serve`). It wakes every 30 s (`dashboard.tick_seconds`); each source has its own interval (`mail.poll_minutes: 1`,
`calendar.poll_minutes: 2`, `linkedin.poll_minutes: 10`). Each cycle: ingest new events → re-prioritize what is
waiting (stage changes, expired or conflicting times are re-drafted, priority scores recomputed) → process new
events (auto-act under your rules, or queue for you) → retry paced LinkedIn sends → learn → push the update to the
window. Calendar watching covers new invitations and job meetings that were moved, cancelled, or declined;
missed calls and voicemails arrive through email notifications (Google Voice or carrier voicemail-to-email).

## Requirements traceability

| Requirement (from the inception conversation) | Where |
| --- | --- |
| Manage all recruiter conversations, async, walk away and get briefed | `orchestrator.py`, `briefing.py`, dashboard |
| Read and reply on LinkedIn, at human pace, through your own session | `servers/linkedin.py` (upstream proxy + pacing) |
| Read and reply to email; email and calendar via MCP servers | `servers/mail.py`, `servers/calendar.py`, `mcp_hub.py` |
| Check schedule, share availability, book, detect conflicts | `agents/scheduler.py`, `scheduling.py`, executor re-check |
| Answer technology / years questions from the markdown CV via a subagent | `servers/cv.py`, `cvparse.py`, `agents/cv_analyst.py` |
| Always push toward landing the position | drafter objective, reviewer job-advancement score, follow-ups |
| Keep a log of every opportunity | `pipeline.py`, Pipeline tab, Notion mirror |
| Update me on upcoming events | Upcoming meetings, meeting reminders (`notify.py`) |
| Draft-and-approve; decide everything, then execute in one batch | `decisions.py` (staged), `executor.run_batch` ("go") |
| "Don't ask again" for this type of decision/question | `decisions.dont_ask_again`, `rules.py` |
| Decision Router reads the Notion database and the decision log | `agents/router.py`, `pipeline.notion_pull`, calibration from the ledger |
| Learn from every recorded decision, become more confident over time | `ledger.py`, `agents/learner.py`, `agents/fewshot.py`, preferences |
| Only paid tool: Claude or Grok; harness self-built | `llm/`, everything else local |

## Project layout

```
steward/
  cli.py            command line            web/            local dashboard (FastAPI + one HTML page)
  orchestrator.py   the walk-away loop      executor.py     the only component that acts
  decisions.py      your decisions          briefing.py     what you see
  ledger.py         hash-chained ledger     rules.py        "don't ask again" predicates
  pipeline.py       opportunities + Notion  notify.py       attention budgeting
  agents/           classifier, cv_analyst, scheduler, drafter (+reviewer), router, learner, fewshot, prompts
  servers/          MCP servers: mail, calendar, cv, linkedin, notion
  llm/              anthropic (Claude), grok, offline heuristics, JSON schemas
tests/              unit + end-to-end tests (real MCP subprocesses, sandbox backends)
```

Run the tests:

```bash
.venv/Scripts/python.exe -m pytest -q
```

## Status and limits

- Verified end to end against the sandbox backends, with the offline provider, through real MCP server
  subprocesses (26 tests, including calls, meeting changes, re-prioritization, the dashboard API, and the
  subscription provider with a fake `claude`), and in the dashboard itself. The subscription path
  (`provider: claude-code`) was also run for real on a Claude Pro plan: triage, draft, and review of a sandbox
  message. The API-key path's request format was verified against the SDK with a mock transport only.
- The IMAP/SMTP, Google Calendar, Notion, and LinkedIn-upstream backends are written against those services'
  documented APIs but have not been exercised against live accounts yet. Run `steward doctor` first, and start with
  everything on "Ask me" (the default) until you trust the drafts.
- The LinkedIn upstream adapter normalizes common response shapes; a specific toolkit may need its tool names (and
  occasionally its field names) mapped.
- Outlook calendars are not built in; any calendar MCP server can be plugged in via `calendar.server`.
