#!/usr/bin/env bash
# Seed gitea with a fixture project for the volunteer rig.
#
# Creates: admin user, API token, fixture repo with OSI license +
# .bernstein/volunteer.json, and two issues labelled volunteer-ok.
# Fails loudly on any auth error (401) so a bad token never produces
# an empty-but-green fixture project.
set -euo pipefail

GITEA_URL="${GITEA_URL:-http://gitea:3000}"
ADMIN_USER="bernstein"
ADMIN_PASS="volunteer-rig-pass"
RIG_BIN=$(cd -- "$(dirname -- "$0")" && pwd)
FIXTURES_DIR="${FIXTURES_DIR:-/fixtures}"
REPO_OWNER="bernstein"
REPO_NAME="fixture-project"
LABEL_NAME="volunteer-ok"

log() { echo "[seed] $(date -u '+%H:%M:%S') $*"; return 0; }

fail() { echo "[seed] ERROR: $*" >&2; exit 1; }

# ── Wait for gitea ────────────────────────────────────────────────────────
log "Waiting for gitea at ${GITEA_URL}..."
until curl -sf "${GITEA_URL}/health" > /dev/null 2>&1; do
    sleep 2
done
log "Gitea is ready."

# ── Admin user is created by gitea container's command ──────────────────
log "Admin user created by gitea container startup."

# ── Get API token ─────────────────────────────────────────────────────────
log "Generating API token..."
TOKEN_JSON=$(curl -sf -X POST "${GITEA_URL}/api/v1/users/${ADMIN_USER}/tokens" \
    -u "${ADMIN_USER}:${ADMIN_PASS}" \
    -H "Content-Type: application/json" \
    -d '{"name":"volunteer-rig-seed","scopes":["write:repository","write:issue"]}') || fail "Failed to get seed token"
TOKEN=$(jq -er '.sha1 | strings | select(length > 0)' <<< "${TOKEN_JSON}") || fail "No seed token returned"
log "Token generated."

# ── Create fixture repo ───────────────────────────────────────────────────
log "Creating fixture repo..."
curl -sf -X POST "${GITEA_URL}/api/v1/user/repos" \
    -H "Authorization: token ${TOKEN}" \
    -H "Content-Type: application/json" \
    -d "{\"name\": \"${REPO_NAME}\", \"private\": false}" > /dev/null

# ── Push fixture files ─────────────────────────────────────────────────────
log "Cloning fixture repo..."
workdir=$(mktemp -d "${TMPDIR:-/tmp}/volunteer-rig.XXXXXXXX")
trap 'rm -rf -- "$workdir"' EXIT
cd "$workdir"
git clone "http://${ADMIN_USER}:${ADMIN_PASS}@gitea:3000/${REPO_OWNER}/${REPO_NAME}.git" 2>/dev/null || fail "Failed to clone the fixture repo"
cd "${REPO_NAME}"

# Copy fixture files
cp "$FIXTURES_DIR"/LICENSE .
cp "$FIXTURES_DIR"/README.md .
mkdir -p .bernstein
cp "$FIXTURES_DIR"/volunteer.json .bernstein/volunteer.json

git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git config user.name "github-actions[bot]"
git add -A
git commit -m "Seed fixture project with volunteer manifest" 2>/dev/null || true
git branch -M main
git push origin main
log "Fixture repo seeded."

# ── Create label ──────────────────────────────────────────────────────────
log "Creating ${LABEL_NAME} label..."
LABEL_ID=$(curl -sf -X POST "${GITEA_URL}/api/v1/repos/${REPO_OWNER}/${REPO_NAME}/labels" \
    -H "Authorization: token ${TOKEN}" \
    -H "Content-Type: application/json" \
    -d "{\"name\": \"${LABEL_NAME}\", \"color\": \"#00a000\"}" | jq -er '.id | numbers')

# ── Create two issues ─────────────────────────────────────────────────────
log "Creating volunteer issues..."
for i in 1 2; do
    curl -sf -X POST "${GITEA_URL}/api/v1/repos/${REPO_OWNER}/${REPO_NAME}/issues" \
        -H "Authorization: token ${TOKEN}" \
        -H "Content-Type: application/json" \
        -d "{\"title\": \"Volunteer task ${i}\", \"body\": \"This is fixture task ${i}.\", \"labels\": [${LABEL_ID}]}" > /dev/null
done
log "Created 2 issues with ${LABEL_NAME} label."

# ── Assert ────────────────────────────────────────────────────────────────
log "Running assert step..."
"${RIG_BIN}/volunteer-rig-assert"

log "Seed complete. Rig is ready."
