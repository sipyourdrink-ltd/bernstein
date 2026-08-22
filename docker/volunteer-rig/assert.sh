#!/usr/bin/env bash
# Assert the gitea seed succeeded. Fails loudly on any mismatch.
set -euo pipefail

GITEA_URL="${GITEA_URL:-http://gitea:3000}"
ADMIN_USER="bernstein"
ADMIN_PASS="volunteer-rig-pass"
REPO_OWNER="bernstein"
REPO_NAME="fixture-project"
LABEL_NAME="volunteer-ok"

log() { echo "[assert] $(date -u '+%H:%M:%S') $*"; return 0; }
fail() { echo "[assert] FAIL: $*" >&2; exit 1; }

# Get a token
TOKEN_JSON=$(curl -sf -X POST "${GITEA_URL}/api/v1/users/${ADMIN_USER}/tokens" \
    -u "${ADMIN_USER}:${ADMIN_PASS}" \
    -H "Content-Type: application/json" \
    -d '{"name": "volunteer-rig-assert"}' 2>/dev/null) || fail "Failed to get token for assert"
TOKEN=$(echo "${TOKEN_JSON}" | jq -r '.sha1')

# Assert: exactly 2 issues with volunteer-ok label
log "Querying issues with ${LABEL_NAME} label..."
ISSUES=$(curl -sf "${GITEA_URL}/api/v1/repos/${REPO_OWNER}/${REPO_NAME}/issues?labels=${LABEL_NAME}&state=open" \
    -H "Authorization: token ${TOKEN}" 2>/dev/null) || fail "Failed to query issues"

COUNT=$(echo "${ISSUES}" | jq 'length')
if [[ "${COUNT}" -ne 2 ]]; then
    fail "Expected 2 issues with ${LABEL_NAME} label, got ${COUNT}"
fi
log "PASS: Found ${COUNT} issues with ${LABEL_NAME} label."

# Assert: manifest is fetchable and valid
log "Fetching manifest from repo..."
MANIFEST=$(curl -sf "${GITEA_URL}/api/v1/repos/${REPO_OWNER}/${REPO_NAME}/raw/.bernstein/volunteer.json?ref=main" \
    -H "Authorization: token ${TOKEN}" 2>/dev/null) || fail "Failed to fetch manifest from repo"

if ! echo "${MANIFEST}" | jq -e '.license' > /dev/null 2>&1; then
    fail "Manifest is not valid JSON or missing license field"
fi
log "PASS: Manifest is fetchable and valid."

log "All asserts passed."