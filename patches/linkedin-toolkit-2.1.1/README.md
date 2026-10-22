# linkedin-toolkit 2.1.1: make LinkedIn messages send

linkedin-toolkit 2.1.1 (the latest release as of 2026-10-22) cannot send LinkedIn messages: LinkedIn
refuses its `createMessage` request, which the extension reports as "LinkedIn did not answer the
unverified 'createMessage' endpoint in a shape this build understands".

`send-message.patch` changes the extension (MIT licensed) so it sends the way linkedin.com's own
messenger does. Verified on 2026-10-22 with two real replies, each of which arrived once: one to a
connection, and one to a recruiter who is not a connection (in his existing conversation).

- **trackingId** is 16 random bytes as a 16-character string, not base64. The base64 form was the
  cause of LinkedIn's bare `400`.
- Every message carries an **originToken** (a UUID).
- **Replies go into the existing conversation** (`conversationUrn`). LinkedIn allows that even with
  people who are not connections, such as a recruiter who wrote first or an InMail. It allows a new
  conversation (`hostRecipientUrns`) only with connections, which caused the `422
  RECIPIENT_NOT_FIRST_DEGREE_CONNECTION`.
- **Finding that conversation:**
  - It is found by the person's member id (`ACoAA...`) as the conversation list reports it. That is
    the id Job Sourcer passes, so no profile lookup is needed, which also saves a profile visit
    from the daily quota.
  - LinkedIn returns 20 conversations per request, so the toolkit's own inbox mirror is searched
    first, then LinkedIn page by page.
- **Headers:** the request uses the messenger's (`accept: application/json`,
  `content-type: text/plain`).
- **Errors:** when LinkedIn refuses an unverified endpoint, the error now includes what LinkedIn
  answered. It also says whether an existing conversation was found.
- **Version:** the manifest version becomes **2.1.1.4**, so you can see that the patched code is the
  one running.

## Apply it (again after re-running the toolkit's setup, which restores the original files)

```bash
cd "$USERPROFILE/.linkedin-toolkit/extension"
git apply "<path to this repo>/patches/linkedin-toolkit-2.1.1/send-message.patch"
```

Then make Edge load it. Edge keeps running the code it loaded last, even across a browser restart:

1. Open `edge://extensions`.
2. Turn on **Developer mode** (bottom of the left sidebar).
3. Click **Reload** on the LinkedIn Toolkit card. The card should show version **2.1.1.4**.

Job Sourcer's LinkedIn check (at startup, and *Check LinkedIn now* in the Logs tab) reports two
problems: an extension without this patch, and a patched extension that Edge has not reloaded yet
(different version on disk and running).
