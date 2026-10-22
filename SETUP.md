# Setting up The Steward with your real accounts

Your workspace already exists at `workspace/`:

- `workspace/cv/cv.md` is your resume (copied from `Bernardo/Resume En.md`). Edit it there; changes apply to the
  next message.
- `workspace/steward.yaml` already has your name, email, headline, and Gmail settings filled in from the resume.
- `workspace/.env` holds secrets. You will normally fill it from the dashboard, never by hand.

Everything below is done **once**, from the dashboard's **Setup** tab. No terminal is needed except for the
one LinkedIn command in step 6.

## 0. Open Steward

Double-click **`Start Steward.cmd`** in the project folder. It starts Steward in the background and opens its
window (Edge/Chrome "app" window). Go to the **Setup** tab. Each step shows *Ready*, *Needs you*, or *Optional*.

(To play with fake data first, double-click **`Start Steward Demo.cmd`** instead.)

## What I need from you (summary)

| # | What | Where you get it | Cost | Required |
| --- | --- | --- | --- | --- |
| 1 | Profile facts: time zone, notice period, work authorization, relocation stance, salary policy, target roles, locations, LinkedIn URL | You | free | yes |
| 2 | Claude: your Pro subscription through Claude Code (already signed in) | nothing to do | included in Pro | done |
| 3 | Gmail App Password | myaccount.google.com | free | yes |
| 4 | Google Calendar OAuth client (a JSON file) + one click to connect | console.cloud.google.com | free | yes |
| 5 | linkedin-toolkit Chrome extension + pairing | your Chrome | free | for LinkedIn |
| 6 | Notion integration secret + a page | notion.so | free | optional |
| 7 | ntfy topic or Telegram bot | ntfy.sh / Telegram | free | optional |
| 8 | A phone number that emails you voicemails (Google Voice or carrier voicemail-to-email) | voice.google.com / your carrier | free | for calls |

Nothing is sent anywhere except: Claude (to reason about messages), your own email/calendar/LinkedIn/Notion
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

Steward uses your **Claude Pro subscription**, not an API key. It runs the official Claude Code program (already
installed and signed in on this computer as bmondragonbrozon@gmail.com, Pro plan) in non-interactive mode, the
same way you could from a terminal. Setup step 3 shows *Ready*; press **Test connection** to see it answer.

- **No API key and no extra cost.** Usage counts toward your Pro plan's limits.
- **Your login stays with Claude Code.** Steward never reads, stores, or forwards your Claude credentials. This is
  the use Anthropic permits for subscriptions: you, signed in to the unmodified Claude Code program. Each call has
  no tools, loads none of your Claude Code settings, hooks, or MCP servers, and saves no session history.
- **Usage limits.** Each new recruiter message takes three short calls (triage, draft, review), about 25 seconds.
  If your plan's usage limit is reached, Steward pauses drafting until the reset time shown in a yellow banner. New
  messages are still collected and are drafted automatically after the reset; none are lost or marked failed.
- **Model.** Opus gives the best drafts. Choose *sonnet* in Setup step 3 to make your limits last longer.

If Claude Code ever signs out: open a terminal, run `claude`, type `/login`, and choose your Claude account.

(Prefer an API key instead? Set the provider to *anthropic* in Setup step 3 and paste a key from
console.anthropic.com.)

## 3. Email (Setup → step 4)

Your address (bmondragonbrozon@gmail.com) and Gmail's servers are already filled in.

1. Turn on 2-Step Verification: https://myaccount.google.com/security
2. Create an App Password: https://myaccount.google.com/apppasswords (name it "Steward"). Copy the 16 characters.
3. Paste it into **Email app password** in Setup step 4, press **Save**, then **Test connection**.

If the test fails, check Gmail **Settings → See all settings → Forwarding and POP/IMAP → IMAP access: Enable**.

Steward reads INBOX every minute without marking anything as read. Unrelated mail is filtered out by keywords
before any AI call. If you prefer, create a Gmail filter that labels recruiter mail `Recruiters` and type
`Recruiters` in the *Folder / Gmail label* field.

## 4. Google Calendar (Setup → step 5)

One-time Google Cloud setup (about 5 minutes):

1. Open https://console.cloud.google.com, create a project named **Steward**.
2. **APIs & Services → Library**: search **Google Calendar API**, press **Enable**.
3. **APIs & Services → OAuth consent screen** (Google Auth Platform): choose **External**, app name "Steward",
   your email as support and developer contact. Under **Audience → Test users**, add your Gmail address.
4. **Clients (Credentials) → Create client → OAuth client ID → Application type: Desktop app**. Download the JSON.
5. Save that file as `workspace/secrets/google_credentials.json` (the exact path is shown in Setup step 5).
6. In Setup step 5, press **Connect Google Calendar**. A Google page opens: choose your account. Google warns
   that the app is unverified; it is your own app, so choose **Advanced → Go to Steward**, then allow access.
7. Press **Test connection**.

**Avoid weekly re-connects:** while the consent screen is in *Testing*, Google expires the connection after 7
days. In **OAuth consent screen → Audience**, press **Publish app** (no review is needed for personal use).

Also set your working hours and the buffer you want around meetings in the same step.

## 5. Phone calls

Steward cannot listen to your phone, but it sees missed calls and voicemails that arrive **by email**:

- **Google Voice** (free, US): https://voice.google.com, then enable **Settings → Voicemail → Get voicemail via email**.
  Put that number on your CV and LinkedIn.
- Or your carrier's **voicemail-to-email** feature.

When a recruiter calls, Steward matches the caller to the opportunity by phone number, company, or name, and
drafts "Sorry I missed your call, here are times that work" in their email or LinkedIn thread, marked high
priority. If it cannot tell who called, it puts a "call them back" item at the top of your briefing.

## 6. LinkedIn (Setup → step 6)

Steward talks to LinkedIn only through **your own logged-in browser** (Microsoft Edge or Chrome), via the
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
4. In Steward's Setup step 6, press **Use linkedin-toolkit**. Then paste the same pairing token into
   **LinkedIn toolkit pairing token** and press **Save**. To print the token again:

   ```bash
   npx -y -p linkedin-toolkit-mcp lit config get token --reveal
   ```

5. Press **Test connection**. It confirms the bridge and the extension are connected and shows the toolkit tools
   Steward uses: `linkedin_get_conversations` to list your inbox, `linkedin_get_messages` to read a thread, and
   `linkedin_send_message` to reply to the person (by their LinkedIn `publicId`).
6. The toolkit queues every send for its own approval (Copilot mode). Either approve those in its popup, or turn
   on its **Autopilot** in the popup so the replies you already approved in Steward go out at its human pace.

**Steward runs the toolkit's bridge for you.** The extension connects to a long-running local bridge
(`lit serve --http`, on 127.0.0.1 ports 47829/47830). Steward starts it when it starts, restarts it if it stops,
and talks to it at `http://127.0.0.1:47830/mcp` with your pairing token. Its errors are logged to
`workspace/data/logs/linkedin-bridge.log`.

Keep Edge open and signed in to LinkedIn: the extension only works while the browser runs. (Edge can keep
extensions running after you close its windows: **Settings → System and performance → Continue running background
extensions and apps when Microsoft Edge is closed**.) On its first check Steward only looks at threads active in the
past 7 days. It then checks every 10 minutes and sends at most 8 messages an hour and 30 a day, only during
business hours. Any LinkedIn automation carries some account risk.

## 7. Notion (optional, Setup → step 7)

Steward always keeps your pipeline locally (Pipeline tab). Notion is an optional mirror you can also edit: if
you move an opportunity to "Offer" there, Steward stops acting on its own for that thread.

1. Go to https://www.notion.so/profile/integrations → **New integration** (type *Internal*), name it "Steward",
   copy the **Internal Integration Secret**.
2. Paste it into **Notion integration secret** in Setup step 7 and press **Save**.
3. In Notion, create a page called "Job Search". On that page, open **••• → Connections** and add "Steward".
4. Copy the page link (**Share → Copy link**), paste it into Setup step 7, press **Create the pipeline database**.

## 8. Phone notifications (optional, Setup → step 8)

The dashboard updates live, and the **🔔 Desktop alerts** button gives Windows notifications. To get alerts on
your phone too:

- **ntfy**: install the ntfy app, subscribe to a long random topic name, and paste `https://ntfy.sh/<topic>` in
  Setup step 8.
- **Telegram**: create a bot with @BotFather, paste its token, and paste your chat id.

## 9. Start when you log in (Setup → step 9)

Turn this on so Steward keeps watching even when its window is closed. Turning it off removes the startup entry.

---

## How it works day to day

- Steward wakes up every **30 seconds** and checks email every **1 minute**, your calendar every **2 minutes**,
  and LinkedIn every **10 minutes** (intervals are in `steward.yaml`: `mail.poll_minutes`, `calendar.poll_minutes`,
  `linkedin.poll_minutes`). Only new messages cost AI calls.
- **Every new event is processed immediately**: a recruiter email, a LinkedIn message, a calendar invitation, a
  meeting that was moved, cancelled, or declined, a missed call or voicemail. Steward then either:
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
- **Steer it in your own words**: each item has *"Or tell Steward what to do instead"*. Write the instruction,
  press *Revise with my instruction*, and Steward rewrites the reply and any meeting, task, or stage changes, then
  shows the new version (marked ✎ revised) for your approval. Each item also has *"Teach Steward a rule"* for how
  it should respond next time, applied to messages like this one, one company, one recruiter, or everything.
- **Tasks**: when a recruiter asks you to take an assessment, fill a form, prepare a document, or apply through
  another link, the item shows a suggested task. *Add to my tasks* puts it in the **Tasks** tab (check it off when
  done; you can also add your own). Deadlines appear under *Upcoming meetings & deadlines*, you get reminders a day
  and two hours before (if notifications are on), and items with close deadlines move up the queue. Due times use
  the time zone from your profile.
- Join the meetings it books (they appear under *Upcoming meetings & deadlines* with join links).

All the CLI commands still work (`steward brief`, `steward go`, ...) if you ever want them.
