# GitHub App Setup for Bernstein

## Prerequisites

- A running Bernstein task server (`bernstein start`)
- A publicly accessible URL for webhooks (use ngrok for local dev)
- A GitHub account with permission to create Apps

## Step-by-step setup

### 1. Create the GitHub App

Go to https://github.com/settings/apps/new and configure:

- **App name**: `bernstein-orchestrator` (or your preferred name)
- **Homepage URL**: Your repo URL
- **Webhook URL**: `https://YOUR_SERVER/webhooks/github`
- **Webhook secret**: Generate a strong random string

### 2. Set permissions

Under "Permissions":

| Permission    | Access      |
|---------------|-------------|
| Issues        | Read & Write |
| Pull requests | Read & Write |
| Contents      | Read         |

### 3. Subscribe to events

Check these events:

- Issues
- Pull request
- Pull request review comment
- Push

### 4. Generate a private key

After creating the app, scroll to "Private keys" and click "Generate a private key". Save the `.pem` file securely.

### 5. Install the App

Go to the App's page and click "Install App", then select the repositories you want Bernstein to manage.

Note the **Installation ID** from the URL after installing (the number in `/installations/XXXXX`).

### 6. Configure environment variables

```bash
export GITHUB_APP_ID=123456
export GITHUB_APP_PRIVATE_KEY=/path/to/private-key.pem
export GITHUB_WEBHOOK_SECRET=your-webhook-secret
```

### 7. Verify

```bash
bernstein github test-webhook
```

## Event-to-task mapping

| GitHub Event                  | Bernstein Action                         |
|-------------------------------|------------------------------------------|
| Issue opened                  | Create task (priority from labels)       |
| Issue labeled `evolve-candidate` | Create evolution/upgrade proposal task |
| PR review comment (actionable)| Create fix task                          |
| Push to branch                | Create QA verification task              |

## Label conventions

Priority labels:
- `bug`, `critical`, `security` -> priority 1
- `enhancement`, `feature` -> priority 2
- `docs`, `documentation`, `chore` -> priority 3

Role labels:
- `backend` -> role: backend
- `frontend` -> role: frontend
- `qa` -> role: qa
- `security` -> role: security
- `docs` -> role: docs

## Local development with ngrok

```bash
# Terminal 1: Start Bernstein
bernstein start

# Terminal 2: Expose via ngrok
ngrok http 8052

# Use the ngrok URL as your webhook URL
# e.g. https://abc123.ngrok.io/webhooks/github
```
