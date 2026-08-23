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
ADMIN_EMAIL="admin@bernstein.test"
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
TOKEN_JSON=$(curl -sf -X POST "${GITEA_URL}/api/v1/repos" \
    -H "Authorization: token ${TOKEN}" \
    -H "Content-Type: application/json" \
    -d "{\"name\": \"${REPO_NAME}\", \"private\": false, \"auto_init\": true}" > /dev/null 2>&1 || true

TOKEN=$(echo "${TOKEN_JSON}" | jq -r '.sha1')
if [[ -z "${TOKEN}" || "${TOKEN}" == "null" ]]; then
    fail "No token returned. Check admin credentials."
fi
log "Token generated."

# ── Create fixture repo ───────────────────────────────────────────────────
log "Creating fixture repo..."
curl -sf -X POST "${GITEA_URL}/api/v1/repos" \
    -H "Authorization: token ${TOKEN}" \
    -H "Content-Type: application/json" \
    -d "{\"name\": \"${REPO_NAME}\", \"private\": false}" > /dev/null 2>&1 || true

# ── Push fixture files ─────────────────────────────────────────────────────
log "Cloning fixture repo..."
cd /tmp
rm -rf "${REPO_NAME}"
git clone "http://${ADMIN_USER}:${ADMIN_PASS}@gitea:3000/${REPO_OWNER}/${REPO_NAME}.git" 2>/dev/null || true
cd "${REPO_NAME}"

# Copy fixture files
cp /fixtures/LICENSE .
cp /fixtures/README.md .
mkdir -p .bernstein
cp /fixtures/volunteer.json .bernstein/volunteer.json

git config user.email "rig@bernstein.test"
git config user.name "Volunteer Rig"
git add -A
git commit -m "Seed fixture project with volunteer manifest" 2>/dev/null || true
git branch -M main
git push origin main
log "Fixture repo seeded."

# ── Create label ──────────────────────────────────────────────────────────
log "Creating ${LABEL_NAME} label..."
curl -sf -X POST "${GITEA_URL}/api/v1/repos/${REPO_OWNER}/${REPO_NAME}/labels" \
    -H "Authorization: token ${TOKEN}" \
    -H "Content-Type: application/json" \
    -d "{\"name\": \"${LABEL_NAME}\", \"color\": \"#00a000\"}" > /dev/null 2>&1 || true

# ── Create two issues ─────────────────────────────────────────────────────
log "Creating volunteer issues..."
for i in 1 2; do
    curl -sf -X POST "${GITEA_URL}/api/v1/repos/${REPO_OWNER}/${REPO_NAME}/issues" \
        -H "Authorization: token ${TOKEN}" \
        -H "Content-Type: application/json" \
        -d "{\"title\": \"Volunteer task ${i}\", \"body\": \"This is fixture task ${i}.\", \"labels\": [\"${LABEL_NAME}\"]}" > /dev/null
done
log "Created 2 issues with ${LABEL_NAME} label."

# ── Assert ────────────────────────────────────────────────────────────────
log "Running assert step..."
/usr/local/bin/volunteer-rig-assert

log "Seed complete. Rig is ready."
