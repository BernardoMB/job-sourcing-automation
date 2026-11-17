# Setting up Job Sourcer with your real accounts

Your workspace already exists at `workspace/`:

- `workspace/cv/cv.md` is your resume (copied from `Bernardo/Resume En.md`). Edit it there; changes apply to the
  next message.
- `workspace/steward.yaml` already has your name, email, headline, and Gmail settings filled in from the resume.
- `workspace/.env` holds secrets. You will normally fill it from the dashboard, never by hand.

Everything below is done **once**, from the dashboard's **Setup** tab. No terminal is needed except for the
LinkedIn command in step 6, the WhatsApp command in step 7, and the Playwright install command in step 8.

## 0. Open Job Sourcer

Double-click **`Start Job Sourcer.cmd`** in the project folder (or the **Job Sourcer** shortcut on your Desktop or in
the Start menu; create them once with `steward shortcut`). It starts Job Sourcer in the background and opens its
window. Go to the **Setup** tab. Each step shows *Ready*, *Needs you*, or *Optional*.

(To play with fake data first, double-click **`Start Job Sourcer Demo.cmd`** instead.)

## What I need from you (summary)

| # | What | Where you get it | Cost | Required |
| --- | --- | --- | --- | --- |
| 1 | Profile facts: time zone, notice period, work authorization, relocation stance, salary policy, target roles, locations, LinkedIn URL | You | free | yes |
| 2 | Claude: your Pro subscription through Claude Code (already signed in) | nothing to do | included in Pro | done |
| 3 | Gmail App Password | myaccount.google.com | free | yes |
| 4 | Google Calendar OAuth client (a JSON file) + one click to connect | console.cloud.google.com | free | yes |
| 5 | linkedin-toolkit Chrome extension + pairing | your Chrome | free | for LinkedIn |
| 5b | Scan one QR code with WhatsApp (`steward whatsapp-link`) | your phone | free | for WhatsApp |
| 6 | Notion: install Playwright once, then sign in to Notion once in the Edge window Job Sourcer opens | notion.so | free | optional |
| 7 | ntfy topic or Telegram bot | ntfy.sh / Telegram | free | optional |
| 8 | A phone number that emails you voicemails (Google Voice or carrier voicemail-to-email) | voice.google.com / your carrier | free | for calls |

Nothing is sent anywhere except: Claude (to reason about messages), your own email/calendar/LinkedIn/WhatsApp/Notion
accounts, and your notification service if you set one. Notion is read by Edge running on this computer with
your own Notion session; no Notion integration or API token is used.

---

## 1. Your profile (Setup → step 1)

These are the **only facts about you, besides your CV, that drafts are allowed to state**. Fill in:

- **Time zone**: click the suggested *This computer* link, or type an IANA name such as `America/Mexico_City`.
- **Target roles** and **preferred locations** (comma-separated). The target roles were pre-filled from your
  resume headline; adjust them.
- **Work authorization** (for example "Mexican citizen; requires TN/H-1B sponsorship" or "US permanent resident").
- **Notice period / start date** (for example "I can start two weeks after accepting an offer.").
- **Relocation stance**, **remote preference**, **LinkedIn profile URL**, **signature**.
- **Salary policy**: what drafts say when asked about pay. The default never states a number and asks for the
  budgeted range. Salary questions are always escalated to you anyway.
- **Your salary expectation (private)**: shown only to you in the briefing context; never sent.

Press **Save profile**.

> Your resume includes detailed descriptions of the Neolinx interception systems. If you would rather
> recruiters never see lines from that section quoted, add this to `workspace/steward.yaml` (the years
> still count toward your experience):
> ```yaml
> cv:
>   exclude_sections: ["Neolinx"]
> ```

## 2. Claude: your Pro subscription (Setup → step 3) — already done

Job Sourcer uses your **Claude Pro subscription**, not an API key. It runs the official Claude Code program (already
installed and signed in on this computer as bmondragonbrozon@gmail.com, Pro plan) in non-interactive mode, the
same way you could from a terminal. Setup step 3 shows *Ready*; press **Test connection** to see it answer.

- **No API key and no extra cost.** Usage counts toward your Pro plan's limits.
- **Your login stays with Claude Code.** Job Sourcer never reads, stores, or forwards your Claude credentials. This is
  the use Anthropic permits for subscriptions: you, signed in to the unmodified Claude Code program. Each call has
  no tools, loads none of your Claude Code settings, hooks, or MCP servers, and saves no session history.
- **Usage limits.** Each new recruiter message takes three short calls (triage, draft, review), about 25 seconds.
  If your plan's usage limit is reached, Job Sourcer pauses drafting until the reset time shown in a yellow banner. New
  messages are still collected and are drafted automatically after the reset; none are lost or marked failed.
- **Model.** Opus gives the best drafts. Choose *sonnet* in Setup step 3 to make your limits last longer.

If Claude Code ever signs out: open a terminal, run `claude`, type `/login`, and choose your Claude account.

(Prefer an API key instead? Set the provider to *anthropic* in Setup step 3 and paste a key from
console.anthropic.com.)

## 3. Email (Setup → step 4)

Your address (bmondragonbrozon@gmail.com) and Gmail's servers are already filled in.

1. Turn on 2-Step Verification: https://myaccount.google.com/security
2. Create an App Password: https://myaccount.google.com/apppasswords (name it "Job Sourcer"). Copy the 16 characters.
3. Paste it into **Email app password** in Setup step 4, press **Save**, then **Test connection**.

If the test fails, check Gmail **Settings → See all settings → Forwarding and POP/IMAP → IMAP access: Enable**.

Job Sourcer reads INBOX every minute without marking anything as read. Unrelated mail is filtered out by keywords
before any AI call. If you prefer, create a Gmail filter that labels recruiter mail `Recruiters` and type
`Recruiters` in the *Folder / Gmail label* field.

- **Replies go back on the channel the message came from**: an email gets an email reply, a LinkedIn message a
  LinkedIn reply.
- **"Thank you for applying" emails are filed**: a company only confirming it received your application is
  marked as read and moved to your **Jobs** label (no AI call, no draft). One that also asks something of you (a
  video interview, an assessment, scheduling), turns you down, or makes an offer goes through the normal flow.
  Settings: `mail.file_application_acks`, `mail.applications_label`.
- **Your inbox is cleaned up once you have handled something.** When a reply has really gone out, you rejected an
  item and pressed Go, you discarded it, or you had already answered it yourself, the emails about it are marked as
  read and moved to **Jobs**. That is the email itself and earlier ones in its thread, the LinkedIn "new message"
  notifications for that conversation, and the invitation email for that meeting. Emails that arrived after you
  handled it, and emails whose task is still open, stay in your inbox. Setting: `mail.cleanup_handled`.
- **Rejection emails close the opportunity and are deleted.** An email that only turns you down ("we have decided
  not to move forward") sets the opportunity to **Closed (lost)** in the pipeline and in Notion, withdraws any reply
  still waiting for that opportunity, and is moved to **Trash** (Gmail empties it after 30 days, so it can be
  restored until then). No reply is drafted. A rejection that also asks something (a question, a task, another
  role) is still closed in the pipeline but stays in your inbox with a draft for you. Each one is listed in the
  Logs tab and the ledger. Setting: `mail.delete_rejections` (off: close the opportunity, keep the email).
- **Mail from LinkedIn is never answered by email** (message and InMail notifications, job alerts, digests). When
  one says someone messaged you, Job Sourcer checks LinkedIn right away and drafts the reply there.
- **Recruiters' meeting invitations go on your calendar right away, without asking**: an invitation file (`.ics`,
  as sent by Google, Outlook, Teams, Zoom, Calendly) or an "Add to calendar" link with the time in it (Google,
  Outlook, Yahoo). The recruiter is not sent a reply and nobody is invited; answering Yes/No stays your call. An
  invitation already on your calendar is not added again. If an email offers "Add to Calendar" but its time can't
  be read, the Logs tab says so.

## 4. Google Calendar (Setup → step 5)

One-time Google Cloud setup (about 5 minutes):

1. Open https://console.cloud.google.com, create a project named **Job Sourcer**.
2. **APIs & Services → Library**: search **Google Calendar API**, press **Enable**.
3. **APIs & Services → OAuth consent screen** (Google Auth Platform): choose **External**, app name "Job Sourcer",
   your email as support and developer contact. Under **Audience → Test users**, add your Gmail address.
4. **Clients (Credentials) → Create client → OAuth client ID → Application type: Desktop app**. Download the JSON.
5. Save that file as `workspace/secrets/google_credentials.json` (the exact path is shown in Setup step 5).
6. In Setup step 5, press **Connect Google Calendar**. A Google page opens: choose your account. Google warns
   that the app is unverified; it is your own app, so choose **Advanced → Go to Job Sourcer**, then allow access.
7. Press **Test connection**.

**Avoid weekly re-connects:** while the consent screen is in *Testing*, Google expires the connection after 7
days. In **OAuth consent screen → Audience**, press **Publish app** (no review is needed for personal use).

Also set your working hours and the buffer you want around meetings in the same step.

## 5. Phone calls

Job Sourcer cannot listen to your phone, but it sees missed calls and voicemails that arrive **by email**:

- **Google Voice** (free, US): https://voice.google.com, then enable **Settings → Voicemail → Get voicemail via email**.
  Put that number on your CV and LinkedIn.
- Or your carrier's **voicemail-to-email** feature.

When a recruiter calls, Job Sourcer matches the caller to the opportunity by phone number, company, or name, and
drafts "Sorry I missed your call, here are times that work" in their email or LinkedIn thread, marked high
priority. If it cannot tell who called, it puts a "call them back" item at the top of your briefing.

## 6. LinkedIn (Setup → step 6)

Job Sourcer talks to LinkedIn only through **your own logged-in browser** (Microsoft Edge or Chrome), via the
open-source [linkedin-toolkit](https://github.com/OpenRecruiterTools/linkedin-toolkit) (browser extension + a
local bridge). Node.js is already installed on this computer.

1. In a terminal, run once:

   ```bash
   npx linkedin-toolkit-mcp setup --client print
   ```

   It downloads the extension, prints its folder, and prints a pairing token.
2. In Edge open `edge://extensions` (in Chrome, `chrome://extensions`), turn on **Developer mode**, choose
   **Load unpacked**, and pick that folder.
3. Open the extension's popup → **Settings → Local bridge**, paste the pairing token, and enable the bridge.
4. In Job Sourcer's Setup step 6, press **Use linkedin-toolkit**. Then paste the same pairing token into
   **LinkedIn toolkit pairing token** and press **Save**. To print the token again:

   ```bash
   npx -y -p linkedin-toolkit-mcp lit config get token --reveal
   ```

5. Press **Test connection**. It confirms the bridge and the extension are connected and shows the toolkit tools
   Job Sourcer uses: `linkedin_get_conversations` to list your inbox, `linkedin_get_messages` to read a thread, and
   `linkedin_send_message` to reply to the person (by their LinkedIn `publicId`).
6. The toolkit queues every send for its own approval (Copilot mode). Either approve those in its popup, or turn
   on its **Autopilot** in the popup so the replies you already approved in Job Sourcer go out at its human pace.

**Apply the send fix.** linkedin-toolkit 2.1.1 cannot send messages as released. Apply
[`patches/linkedin-toolkit-2.1.1`](patches/linkedin-toolkit-2.1.1/README.md) to the extension, then in
`edge://extensions` turn on **Developer mode** and click **Reload** on LinkedIn Toolkit (the card then shows version
2.1.1.4; Edge keeps running the old code until you do, even across restarts). Re-apply it whenever you re-run the
toolkit's setup. The LinkedIn check in the Logs tab warns when the patch is missing or not reloaded yet.

**Job Sourcer runs the toolkit's bridge for you.** The extension connects to a long-running local bridge
(`lit serve --http`, on 127.0.0.1 ports 47829/47830). Job Sourcer starts it when it starts, restarts it if it stops,
and talks to it at `http://127.0.0.1:47830/mcp` with your pairing token. Its errors are logged to
`workspace/data/logs/linkedin-bridge.log`.

Keep Edge open and signed in to LinkedIn: the extension only works while the browser runs. (Edge can keep
extensions running after you close its windows: **Settings → System and performance → Continue running background
extensions and apps when Microsoft Edge is closed**.) On its first check Job Sourcer only looks at threads active in the
past 7 days. After that, each check also re-reads the last 48 hours (`linkedin.recheck_hours`), because a thread can
show up in the inbox after newer messages elsewhere (InMails often do), and a LinkedIn "sent you a message" email
makes Job Sourcer open that exact conversation, however old. A message is never recorded twice. It then checks every 10 minutes and sends at most 8 messages an hour and 30 a day, only during
business hours. Any LinkedIn automation carries some account risk.

## 7. WhatsApp (Setup → step 7)

Job Sourcer reads and answers recruiters on WhatsApp the same way it does on LinkedIn: each new message in a
one-to-one chat about a job opportunity is drafted, routed, and put in front of you (or handled under a rule you
approved), and the reply goes back in the same chat. **Every other chat is ignored completely**: a chat that does not
mention a job (in English or Spanish) is never stored, logged, or sent to Claude; one that mentions work but turns
out to be personal is dropped after triage, with nothing of it kept. Group chats are never read, and Job Sourcer
never writes in a group.

It connects through the open-source [whatsapp-mcp-ts](https://github.com/jlucaso1/whatsapp-mcp-ts), linked to your
account as a WhatsApp **linked device** (like WhatsApp Web). Node.js 23.10 or newer and git are needed (both are
already installed on this computer).

1. In a terminal in the project folder, run once:

   ```bash
   .venv/Scripts/steward.exe whatsapp-link
   ```

   It installs whatsapp-mcp-ts in `%USERPROFILE%\.whatsapp-mcp-ts`, sets Job Sourcer to use it, and shows a QR
   code. Out of the box whatsapp-mcp-ts opens its QR code on quickchart.io, a public website; Job Sourcer removes
   that line, so the code only appears in your terminal.
2. On your phone, open WhatsApp → **Settings → Linked devices → Link a device**, and scan the code. Wait until the
   terminal says *WhatsApp linked* (it stays connected a little longer to receive your recent chats).
3. In Setup step 7, press **Test connection**.

Your WhatsApp login and a copy of your messages stay in that folder on this computer (`auth_info/` and
`data/whatsapp.db`). To unlink, remove "whatsapp-mcp-ts" under **Linked devices** on your phone, or delete
`auth_info` and set the backend to `disabled`.

On its first check Job Sourcer only looks at chats active in the past 7 days. It then checks every 5 minutes, and
each check takes about 20 seconds longer while the linked device reconnects and receives what arrived meanwhile.
Replies go out at most 8 an hour and 30 a day, only during the reply hours (09:00 to 19:00 by default). Linked-device
tools are not an official WhatsApp API: like any automation of a personal account, they carry some risk of the
account being restricted, so keep the caps low.

## 8. Notion events → your calendar (optional, Setup → step 8)

Job Sourcer reads Notion the way you do: through a **browser signed in to your Notion account**. It does not use a
Notion integration, an API token, or Notion's public API. [Playwright](https://playwright.dev/python/) drives
your installed **Microsoft Edge** with its own separate profile, opens the Notion web app (`app.notion.com`)
**headless** (no window, nothing on your screen), visits **every workspace** your account belongs to, collects the
dated items in your databases, and adds the ones that are not on your calendar yet to the Google Calendar from
step 4.

**One-time install** (in a terminal, from the project folder):

```powershell
.venv\Scripts\pip install -e ".[notion]"
```

That installs Playwright for Python. No browser download is needed: Job Sourcer uses the Edge already on this
computer. (To use Chrome instead, pick *chrome* as the browser in Setup step 8. *chromium* means Playwright's own
browser, which needs `.venv\Scripts\python -m playwright install chromium` and may be blocked by company policy.)

**Turn it on and sign in once:**

1. In Setup step 8, switch on **Sync Notion events to my calendar** and press **Save**.
2. Press **Sync now**. Job Sourcer checks whether its saved Notion session is still signed in:
   - **Not signed in** (always true the first time): an Edge window opens on the Notion login page. Sign in as
     you usually do (email code, Google, or Apple; complete any two-step check). As soon as Notion lets you in,
     Job Sourcer saves the session and **closes the window by itself**. You do not need to do anything else.
   - **Already signed in**: no window opens. The sync runs in the background.
3. Setup step 8 shows *Ready*, the time of the last sync, and its result.

**What it keeps:** the browser profile (Notion's sign-in cookies) lives in `workspace/secrets/notion-browser/`. It
is separate from your everyday Edge profile, never sent anywhere, and ignored by git like the rest of
`workspace/`. To sign Job Sourcer out, press **Forget Notion session** in Setup step 8 (or delete that folder); the next
sync opens the login window again.

**When the window appears again:** only when the saved session is no longer signed in (you signed out of all
devices, changed your password, or Notion ended the session). Every other sync stays invisible. If you are not at
the computer, the window waits up to 15 minutes (`notion.login_timeout_minutes`), then that run is logged as
`NEEDS LOGIN` and the next scheduled run asks again. A sync never holds up email, LinkedIn, or calendar checks,
even while the login window waits.

**What gets synced:**

- **Every workspace** your account belongs to is read on every run, including ones you join later: your
  private pages, pages shared with you, your teamspaces, and every page below them (up to 400 pages per
  workspace).
- **Every database with a date property.** The row's date comes from the property named like *Date*, *Date &
  Time*, *When*, or *Start* (else the first date property). If a database has a calendar or timeline view, the
  property that view uses wins. To sync only databases you view as a calendar or timeline, tick **Only databases
  I view as a calendar or timeline in Notion** (`notion.only_calendar_views: true`).
- Items dated from today through the next 60 days (`notion.lookahead_days`). Items with a time become timed events
  (in the time zone Notion stored with them, else your time zone from step 1); items with only a date become
  all-day events that do not block your availability.
- Each calendar event is titled like the Notion item, links back to it in the description, has no attendees (no
  invitations are sent), and is tagged with the Notion page id so it is **never added twice**. If you change the
  date or title in Notion, the existing event is updated. If you delete the event from your calendar, Job Sourcer
  leaves it deleted. Job Sourcer never deletes calendar events; if a Notion item disappears, the log says so and you
  decide.
- Job Sourcer only **reads** Notion. It never edits, creates, or deletes anything there.

**Schedule:** every 15 minutes (`notion.poll_minutes` in `steward.yaml`), plus **Sync now** whenever you want. From a
terminal: `steward notion-sync` runs one sync now and `steward notion-log` shows the end of the log.

### Notion sync logs

Every run writes to **`workspace/data/logs/notion-sync.log`** (one line per step, newest at the bottom). Press
**View sync log** in Setup step 8 to read it in the dashboard (results in color). Each run starts with `START` and
ends with exactly one result line:

- `SYNC OK`: the scrape and the calendar update succeeded, with the counts of workspaces, events found, added,
  updated, and unchanged.
- `SYNC FAILED`: with the reason (for example Edge could not start, Notion did not answer, or Google Calendar
  refused every change). Events already added stay; the next run retries the rest without duplicates.
- `NEEDS LOGIN`: the session was signed out and nobody signed in within 15 minutes.

Every event the sync puts on your calendar gets its own `ADDED` line (title, date and time, workspace / database,
and the Notion link). Other lines: `WORKSPACE` (one per workspace read), `UPDATED` (an event changed in Notion),
`LOGIN` (the login window opened, or you signed in and it closed), `SKIPPED` (you deleted that event from your
calendar), `GONE` (an item left Notion; its event was kept), `NOTE`, and `ERROR` (one item could not be put on the
calendar). A real example:

```text
2026-11-16 22:17:12 START     notion sync (command line; headless; saved session: yes)
2026-11-16 22:17:22 WORKSPACE William Wilson's Space: 4 page(s), 1 database(s) with dates, 0 dated item(s) in range
2026-11-16 22:17:22 WORKSPACE bmondragonbrozon's Space: 3 page(s), 1 database(s) with dates, 0 dated item(s) in range
2026-11-16 22:17:22 WORKSPACE JOB2US: 5 page(s), 2 database(s) with dates, 1 dated item(s) in range
2026-11-16 22:17:23 ADDED     "Your virtual screen at Marriott International" 2026-11-16 10:30-11:00 (America/New_York) [JOB2US / Interviews] https://app.notion.com/415be3ba...
2026-11-16 22:17:23 SYNC OK   3 workspace(s), 1 event(s) found, 1 added, 0 updated, 0 unchanged (11.1 s)
```

When a sync adds events you also get a toast in the dashboard, a desktop alert if you turned them on, and a phone
notification if you set one up in step 9.

**If something goes wrong:**

- *Google sign-in says "This browser or app may not be secure".* Google sometimes blocks sign-in inside
  automated browsers. On the Notion login page, choose **Continue with email** and use the login code Notion
  emails you instead.
- *`SYNC FAILED ... could not start msedge through Playwright`.* Check that Edge is installed, or choose
  *chrome* as the browser in Setup step 8.
- *The log shows `SYNC FAILED` repeatedly.* Notion may have changed its web app. Press **Sync now** once to retry,
  and keep the log for troubleshooting.
- *Step 7 says Playwright is not installed.* Run the install command above, then restart Job Sourcer.

## 9. Notifications (Setup → step 9)

Every check that brings new events (a recruiter message, a call, an invite, a moved or cancelled meeting) plays
`assets/Tethys Nexus 5.mp3` on this computer, even when the window is closed. Mail filtered out as unrelated stays
silent, and so is any WhatsApp chat that is not about a job. Switch it off, or press **Play test sound**, in Setup
step 9. To use another sound, set
`notifications.sound_file` in `steward.yaml`.

The dashboard updates live, and the **🔔 Desktop alerts** button gives Windows notifications. To get alerts on
your phone too:

- **ntfy**: install the ntfy app, subscribe to a long random topic name, and paste `https://ntfy.sh/<topic>` in
  Setup step 9.
- **Telegram**: create a bot with @BotFather, paste its token, and paste your chat id.

## 10. Start when you log in (Setup → step 10)

Turn this on so Job Sourcer keeps watching even when its window is closed. Turning it off removes the startup entry.

---

## How it works day to day

- Job Sourcer wakes up every **30 seconds** and checks email every **1 minute**, your calendar every **2 minutes**,
  LinkedIn every **10 minutes**, and Notion every **15 minutes** (intervals are in `steward.yaml`:
  `mail.poll_minutes`, `calendar.poll_minutes`, `linkedin.poll_minutes`, `notion.poll_minutes`). Only new messages
  cost AI calls; the Notion sync never calls the AI. Its runs are logged in `workspace/data/logs/notion-sync.log`.
- **Every new event is processed immediately**: a recruiter email, a LinkedIn message, a calendar invitation, a
  meeting that was moved, cancelled, or declined, a missed call or voicemail. Job Sourcer then either:
  - **acts by itself** (only under rules you created with "Don't ask again" or accepted from its suggestions),
    listed under *Handled automatically* where you can still say "This was wrong"; or
  - **asks you**, with a ready-to-send draft, in *High priority* or *Needs your decision*.
- **Re-prioritization** runs on every check. The queue is ordered by a priority score that rises with the hiring
  stage, urgency, time pressure (a meeting soon), a recruiter calling you, and waiting time. Drafts whose
  proposed times have passed or were booked over are re-drafted automatically. If an opportunity moves to
  Offer, its waiting drafts become high priority.
- The window updates by itself. You get a toast for each change, a count in the window title, and a desktop
  notification if you enabled it. If you are typing in a draft when something arrives, it waits and shows a
  "New updates" banner instead of disturbing you.
- **Your part**: read the briefing, then Approve / edit / Reject / "Don't ask again", and press **Go**. To send
  each approval immediately instead, switch on **Act on each approval immediately** at the top.
- **Steer it in your own words**: each item has *"Or tell Job Sourcer what to do instead"*. Write the instruction,
  press *Revise with my instruction*, and Job Sourcer rewrites the reply and any meeting, task, or stage changes, then
  shows the new version (marked ✎ revised) for your approval. Each item also has *"Teach Job Sourcer a rule"* for how
  it should respond next time, applied to messages like this one, one company, one recruiter, or everything.
- **Tasks**: when a recruiter asks you to take an assessment, fill a form, prepare a document, or apply through
  another link, the item shows a suggested task. *Add to my tasks* puts it in the **Tasks** tab (check it off when
  done; you can also add your own). Deadlines appear under *Upcoming meetings & deadlines*, you get reminders a day
  and two hours before (if notifications are on), and items with close deadlines move up the queue. Due times use
  the time zone from your profile.
- Join the meetings it books (they appear under *Upcoming meetings & deadlines* with join links).
- **Queue tab**, in sections:
  - **Ready to go**: exactly what **Go** runs. That is decisions you made in the briefing (*Pending
    confirmation*), queued items, and deferred items whose wait is over. The Go button counts only these.
  - **Needs your attention**: failed items. Go never retries a failure, so Re-execute or Discard each one.
  - **Waiting**: deferred until their time, or already on their way.
  - **Everything in the queue**: the full table, with what finished or was closed in the last days. LinkedIn replies wait for
  `linkedin.business_hours`; **Execute** on a deferred item sends it now. Each item has **Actions ▾**:
  **Re-execute** runs it again now (a failed one, or one the LinkedIn toolkit failed or still holds), and
  **Discard** drops it for good (status *Discarded*; one waiting in the toolkit is rejected there too). The
  toolkit's own Copilot mode and hours still apply: in Copilot mode only you can approve a reply in the extension.
- **You can always answer recruiters yourself.** On every check Job Sourcer reconciles open items with what really
  happened: if you already replied on LinkedIn or from your mailbox (it reads your Sent folder's headers), or
  answered an invitation in your calendar, the item is closed as *No longer needed* and nothing is sent. A reply it
  drafted that shows up in the conversation is marked done. Replies handed to the LinkedIn toolkit follow its
  queue: sent, failed (with its reason), or rejected.
- **Online assessments are never missed.** An email inviting you to an assessment (HackerRank, Codility,
  CodeSignal, TestGorilla, ...) becomes an open task with the test link and the deadline, listed under *Upcoming
  meetings & deadlines* with reminders. Reminders for the same test update that task instead of adding another.
  Tick it off in the Tasks tab when you are done.
- **Logs tab**: *What's blocking right now* (deferred, failed, or interrupted actions, decisions pending
  confirmation, Claude paused, a check that failed, LinkedIn not usable), then every check and what triggered it,
  every new message and why one was ignored, and every action with its outcome.
- **LinkedIn is checked when Job Sourcer starts** (and with *Check LinkedIn now* in the Logs tab): it really reads
  your inbox, and for sending it reports what decides it (extension connected and signed in, the send fix applied,
  Copilot or Autopilot, the toolkit's hours, messages left today, how the last send went), plus today's invite,
  visit, and search quotas. The result is a line in the Logs tab, and a blocker until LinkedIn is usable again.

All the CLI commands still work (`steward brief`, `steward go`, ...) if you ever want them.
