# Setting up Job Sourcer with your real accounts

Your workspace already exists at `workspace/`:

- `workspace/cv/cv.md` is your resume (copied from `Bernardo/Resume En.md`). Edit it there; changes apply to the
  next message.
- `workspace/steward.yaml` already has your name, email, headline, and Gmail settings filled in from the resume.
- `workspace/.env` holds secrets. You will normally fill it from the dashboard, never by hand.

Everything below is done **once**, from the dashboard's **Setup** tab. No terminal is needed except for the
one LinkedIn command in step 6.

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
| 6 | Notion integration secret + a page | notion.so | free | optional |
| 7 | ntfy topic or Telegram bot | ntfy.sh / Telegram | free | optional |
| 8 | A phone number that emails you voicemails (Google Voice or carrier voicemail-to-email) | voice.google.com / your carrier | free | for calls |

Nothing is sent anywhere except: Claude (to reason about messages), your own email/calendar/LinkedIn/WhatsApp/Notion
accounts, and your notification service if you set one.

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
past 7 days. It then checks every 10 minutes and sends at most 8 messages an hour and 30 a day, only during
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

## 8. Notion (optional, Setup → step 8)

Job Sourcer always keeps your pipeline locally (Pipeline tab). Notion is an optional mirror you can also edit: if
you move an opportunity to "Offer" there, Job Sourcer stops acting on its own for that thread.

1. Go to https://www.notion.so/profile/integrations → **New integration** (type *Internal*), name it "Job Sourcer",
   copy the **Internal Integration Secret**.
2. Paste it into **Notion integration secret** in Setup step 8 and press **Save**.
3. In Notion, create a page called "Job Search". On that page, open **••• → Connections** and add "Job Sourcer".
4. Copy the page link (**Share → Copy link**), paste it into Setup step 8, press **Create the pipeline database**.

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
  and LinkedIn every **10 minutes** (intervals are in `steward.yaml`: `mail.poll_minutes`, `calendar.poll_minutes`,
  `linkedin.poll_minutes`). Only new messages cost AI calls.
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
