# Microsoft Teams setup

The Teams driver gives operators whose org standardises on Microsoft Teams the
same attested approval surface as Slack, Discord, and Telegram: interactive
Adaptive Cards, HMAC-chained approval events, worktree pinning, and Ed25519
outbound message signing.

## Install the extra

```bash
pip install 'bernstein[teams]'
```

This pulls in the Bot Framework connector SDK. The import is deferred, so the
other chat drivers keep working without it.

## Register a bot

1. In the Azure portal, create an **Azure Bot** resource.
2. Note the **Microsoft App id** and create a **client secret** (app password).
3. Add the **Microsoft Teams** channel to the bot.
4. Point the bot's messaging endpoint at wherever you route inbound activities
   into the bridge.

## Configure credentials

The app id and the client secret rotate independently, so they live in
separate environment variables:

```bash
export BERNSTEIN_TEAMS_TOKEN='<Microsoft App id>'
export BERNSTEIN_TEAMS_APP_PASSWORD='<client secret>'
```

## Run the bridge

```bash
bernstein chat serve --platform=teams
```

The app id may also be passed with `--token`; the app password is only read
from `BERNSTEIN_TEAMS_APP_PASSWORD`.

## What you get

| Capability | Behaviour |
|---|---|
| Commands | A Teams message naming a registered subcommand routes to its handler |
| Approval cards | `push_approval` renders an Adaptive Card with Approve / Reject submit actions; a v2 card body is the verbatim projection of the hashed envelope |
| Attested approvals | Each resolution appends a `chat.teams.approval` audit event covering `(approver, activity_id, decision, tool_call_hash, worktree_id)` |
| Worktree pinning | A resolve from `wt-a` cannot settle an approval bound to a different worktree; the attempt is logged as `chat.teams.approval_rejected` |
| Signed messages | Every outbound message carries an Ed25519 signature over `(install_id, session_id, content_hash)`; the shared verifier confirms authenticity |

The signature envelope is identical to the Slack driver's, so one verifier
covers both surfaces.

## Related

- Approval card reference: `integrations/approval-cards.md`
- Chat bridges overview: `operations/chat-bridges.md`
