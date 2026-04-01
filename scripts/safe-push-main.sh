#!/usr/bin/env bash
set -euo pipefail

# Safe main-branch push helper:
# - keeps local main up to date with origin/main via rebase
# - then performs a normal fast-forward push
# - never uses force push

REMOTE="${1:-origin}"
BRANCH="${2:-main}"

current_branch="$(git rev-parse --abbrev-ref HEAD)"
if [[ "${current_branch}" != "${BRANCH}" ]]; then
  echo "error: current branch is '${current_branch}', expected '${BRANCH}'" >&2
  exit 1
fi

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "error: working tree is dirty; commit or stash first" >&2
  exit 1
fi

git fetch "${REMOTE}" "${BRANCH}"
git rebase "${REMOTE}/${BRANCH}"
git push "${REMOTE}" "${BRANCH}"

echo "safe push complete: ${REMOTE}/${BRANCH}"
