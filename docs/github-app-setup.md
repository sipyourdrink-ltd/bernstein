# GitHub App Setup

Bernstein can receive GitHub webhooks and automatically create tasks from
GitHub events (issues, PRs, pushes, labels).

## Overview

The webhook handler lives at `POST /webhooks/github` on the Bernstein task
server. When GitHub sends a webhook, Bernstein:

1. Verifies the HMAC-SHA256 signature
2. Parses the event type and payload
3. Maps the event to one or more Bernstein tasks
4. Creates the tasks in the task store

## Event mapping

| GitHub event | Condition | Bernstein task |
|---|---|---|
| `issues.opened` | New issue filed | Standard task, priority from labels |
| `issues.labeled` | `evolve-candidate` label added | Evolution/upgrade proposal task |
| `pull_request_review_comment.created` | Actionable comment (contains fix/change/should/etc.) | Fix task, role inferred from file path |
| `push` | Push to any branch | QA verification task |

## Setup

### 1. Create a GitHub App

Go to **Settings > Developer settings > GitHub Apps > New GitHub App**.

Required settings:
- **Webhook URL**: `https://<your-server>/webhooks/github`
- **Webhook secret**: generate a strong random string
- **Permissions**: Issues (Read & Write), Pull requests (Read & Write), Contents (Read)
- **Events**: Issues, Pull request, Pull request review comment, Push

### 2. Install on your repository

After creating the app, click "Install App" and select your repository.

### 3. Configure environment

```bash
export GITHUB_WEBHOOK_SECRET=<your-webhook-secret>
```

For installation token support (optional, for posting comments back to GitHub):

```bash
export GITHUB_APP_ID=<your-app-id>
export GITHUB_APP_PRIVATE_KEY=<path-to-pem-file>
```

### 4. Start the server

```bash
bernstein start
```

### 5. Verify

```bash
bernstein github test-webhook
```

## Local development

Use ngrok to expose your local server:

```bash
ngrok http 8052
```

Then set the ngrok URL as your webhook URL in the GitHub App settings.

## Label conventions

Priority mapping:
- `bug`, `critical`, `security` -- priority 1 (highest)
- `enhancement`, `feature` -- priority 2
- `docs`, `documentation`, `chore` -- priority 3

Role mapping:
- `backend`, `frontend`, `qa`, `security`, `docs` -- mapped directly to Bernstein roles

## Architecture

```
GitHub --> POST /webhooks/github --> verify_signature()
                                 --> parse_webhook()
                                 --> mapper (issue_to_tasks / pr_review_to_task / push_to_tasks / label_to_action)
                                 --> store.create()
```

Source code: `src/bernstein/github_app/`
