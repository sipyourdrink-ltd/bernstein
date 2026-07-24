# Slack webhook integration

Bernstein exposes two HTTP routes that let a Slack app create tasks
directly, without going through the Socket Mode chat bridge: a slash
command receiver and an Events API receiver. Both live in
`core/routes/slack.py` and are registered on the same FastAPI app that
serves `bernstein serve`.

This is a **different integration path** from the interactive chat
bridge documented at [Chat bridges](chat-bridges.md)
(`bernstein chat serve --platform=slack`, Socket Mode, approve/reject
buttons, streamed output). The webhook routes here are one-shot: an
inbound HTTP request creates exactly one task and returns an
acknowledgement. There's no session, no streaming, and no approval UI.

---

## Routes

### `POST /webhooks/slack/commands` - slash commands

Receives a Slack slash command payload (URL-encoded form body), verifies
the request signature, and creates a task from the command text.

- Parses `command`, `text`, `user_id`, `channel_id`, `response_url`,
  `trigger_id`, `thread_ts` from the form body.
- If `text` is non-empty, creates a task via `TaskStore.create()` with
  `title` = first 60 characters of `text`, `description` = full `text`,
  `role="backend"`, `priority=1`, `scope="small"`, and a `slack_context`
  dict carrying `channel_id`, `user_id`, `thread_ts`, `response_url`.
- Responds within Slack's 3-second window with
  `{"response_type": "ephemeral", "text": "Task `<id>` created: ..."}`
  (or a "no task text provided" message if `text` was empty).

### `POST /webhooks/slack/events` - Events API

Receives Slack Events API callbacks.

- `url_verification` events: echoes back the `challenge` value (required
  once, when you register the endpoint in the Slack app config).
- `event_callback` events of type `message`: creates a task only when
  the bot user is `@`-mentioned in the message text. Bot messages and
  `message_changed` subtypes are ignored (loop prevention).
- On an actionable mention, the mention is stripped from the text and
  the remainder becomes the task title/description, with `slack_context`
  carrying `channel`, `user`, `thread_ts`.
- All other event types return `{"ok": true}` without side effects.

This route is also documented from the trigger-routing angle (how it
fits alongside GitHub, OData, and schedule triggers) at
[Trigger sources](trigger-sources.md#two-independent-paths-from-event-to-task);
this page is the Slack-specific reference for both routes together.

---

## Request verification

Both routes verify the Slack HMAC request signature
(`x-slack-request-timestamp` + `x-slack-signature` headers) against a
signing secret, using `verify_slack_signature()` from
`trigger_sources/slack.py`. The secret is read from
`app.state.slack_signing_secret` if the server was started with one, else
from the `SLACK_SIGNING_SECRET` environment variable.

**If no signing secret is configured, verification is skipped entirely**
- both routes accept unsigned requests in that case. Set
`SLACK_SIGNING_SECRET` (or pass `slack_signing_secret=` when building the
app) to enforce verification. A failed verification returns `401`; a
malformed payload returns `400`.

The Events API route additionally reads `SLACK_BOT_USER_ID` from the
environment to recognize `@`-mentions. If it's unset, every message in a
watched channel is treated as a mention (the `<@bot_id>` substring check
is skipped).

---

## Setup

1. Create a Slack app (or reuse the one from [Chat bridges](chat-bridges.md)
   if you also run the Socket Mode driver).
2. Point the app's **Slash Commands** request URL at
   `https://<your-host>/webhooks/slack/commands`.
3. Point the app's **Event Subscriptions** request URL at
   `https://<your-host>/webhooks/slack/events`, complete the
   `url_verification` challenge, and subscribe to the `message.channels`
   (or equivalent) bot event.
4. Set the environment variables on the machine running `bernstein serve`:

   ```sh
   export SLACK_SIGNING_SECRET=...
   export SLACK_BOT_USER_ID=...   # optional; recognizes @-mentions
   ```

5. Run the server: `bernstein serve` (or however your deployment starts
   `core/server/server_app.py`).

---

## Limitations

- No response is posted back into the Slack thread beyond the immediate
  slash-command acknowledgement - task progress is not streamed to
  Slack from these routes. Use the chat bridge if you need that.
- The Events API handler only reacts to `message` events with an
  `@`-mention; it does not parse structured commands out of a mention
  the way the slash-command route does.
- Task fields created here are fixed (`role="backend"`, `priority=1`,
  `scope="small"`) - there is no per-workspace configuration for these
  defaults.

---

## Source

- `src/bernstein/core/routes/slack.py` - both route handlers
  (`slack_slash_command`, `slack_events`).
- `src/bernstein/core/trigger_sources/slack.py` - `verify_slack_signature`,
  `normalize_slack_message`.
- `src/bernstein/core/server/server_app.py` - `slack_signing_secret` app
  state wiring.
- `docs/reference/openapi-reference.md` - flag/route index entries for
  `POST /webhooks/slack/commands` and `POST /webhooks/slack/events`.
