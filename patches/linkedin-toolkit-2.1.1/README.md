# linkedin-toolkit 2.1.1: make LinkedIn messages send

linkedin-toolkit 2.1.1 (the latest release as of 2026-10-22) cannot send LinkedIn messages: its
`createMessage` request is refused by LinkedIn with a bare `400`, which the extension reports as
"LinkedIn did not answer the unverified 'createMessage' endpoint in a shape this build understands".

`send-message.patch` changes the extension (MIT licensed) so it sends the way linkedin.com's own
messenger does. It was verified on 2026-10-22: a reply sent this way arrived in the conversation, once.

- **trackingId** is 16 random bytes as a 16-character string, not base64 (this was the cause of the 400).
- Every message carries an **originToken** (a UUID).
- A reply goes into the **existing 1:1 conversation** (`conversationUrn`); `hostRecipientUrns` is only
  used to start a new one. A bare member id (`ACoAA...`) becomes a full `urn:li:fsd_profile:` urn.
- The request uses the messenger's headers (`accept: application/json`, `content-type: text/plain`).
- When LinkedIn refuses an unverified endpoint, the error now includes what LinkedIn answered.
- The popup reloads the extension once after a patch. Browsers keep running a cached copy of the
  background code until the extension is reloaded, even across a browser restart.

## Apply it (again after re-running the toolkit's setup, which restores the original files)

```bash
cd "$USERPROFILE/.linkedin-toolkit/extension"
git apply "<path to this repo>/patches/linkedin-toolkit-2.1.1/send-message.patch"
```

Then click the LinkedIn Toolkit icon in Edge once. The popup closes by itself while the extension
reloads; reopen it after ~10 seconds and check that the bridge is connected. Setup step 6 in Job
Sourcer warns when the installed extension is not patched.
