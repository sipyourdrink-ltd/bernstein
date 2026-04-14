#!/usr/bin/env bash
# generate-spotlight.sh — Auto-generate a Community Spotlight blog post
# Usage: ./scripts/generate-spotlight.sh --month YYYY-MM [--repo /path/to/repo]
#
# Requires: curl, jq (for GitHub API queries)
set -euo pipefail

MONTH="${1#--month=}"
REPO_ROOT="${2#--repo=}"
REPO_ROOT="${REPO_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || .)}"
OUTPUT_DIR="$REPO_ROOT/docs/community-spotlights"

# --- Helpers ---
info()  { printf '\033[1;34m[INFO]\033[0m  %s\n' "$*"; }
error() { printf '\033[1;31m[ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

[[ -n "$MONTH" ]] || error "Usage: $0 --month YYYY-MM [--repo /path/to/repo]"

# Validate YYYY-MM format
[[ "$MONTH" =~ ^[0-9]{4}-(0[1-9]|1[0-2])$ ]] || error "Invalid month format: $MONTH (expected YYYY-MM)"

PREV_MONTH=$(date -d "$MONTH-01 -1 month" +%Y-%m 2>/dev/null || python3 -c "import datetime; d=datetime.date(int('$MONTH'.split('-')[0]),int('$MONTH'.split('-')[1]),1)-datetime.timedelta(days=1); print(d.strftime('%Y-%m'))")

MONTH_NAME=$(date -d "$MONTH-01" +%B 2>/dev/null || python3 -c "import datetime,calendar; print(calendar.month_name[int('$MONTH'.split('-')[1])])")
YEAR="${MONTH%%-*}"

SINCE="${PREV_MONTH}-01"
UNTIL="$MONTH-01"

info "Generating Community Spotlight for $MONTH_NAME $YEAR"
info "Period: $SINCE → $UNTIL"

# --- Collect contributors ---
CONTRIBUTORS=$(gh api repos/chernistry/bernstein/pulls --paginate \
  -q ".[] | select(.merged_at != null) | select(.merged_at >= \"$SINCE\" and .merged_at < \"$UNTIL\") | {
    author: .user.login,
    title: .title,
    url: .html_url,
    merged_at: .merged_at
  }" 2>/dev/null || echo "[]")

if [[ -z "$CONTRIBUTORS" || "$CONTRIBUTORS" == "[]" ]]; then
  info "No merged PRs found for $MONTH_NAME $YEAR"
  exit 0
fi

COUNT=$(echo "$CONTRIBUTORS" | jq 'length')
info "Found $COUNT merged PR(s)"

# --- Generate markdown ---
mkdir -p "$OUTPUT_DIR"
OUTFILE="$OUTPUT_DIR/${MONTH}.md"

cat > "$OUTFILE" << HEADER
---
title: "Community Spotlight — $MONTH_NAME $YEAR"
date: "${MONTH}-01"
---

# Community Spotlight — $MONTH_NAME $YEAR

Welcome to this month's community spotlight! We're grateful for every contribution that helps make Bernstein better.

## ✨ New Contributors & Notable PRs

HEADER

# Add each contributor
echo "$CONTRIBUTORS" | jq -r '.[] | "- **[@\(.author)](\(.author))** — [\(.title)](\(.url))"' >> "$OUTFILE"

cat >> "$OUTFILE" << FOOTER

## 🙏 Thank You

A huge thank you to all $COUNT contributor(s) who made $MONTH_NAME $YEAR a great month for Bernstein!

> Want to be featured? Contribute to [Bernstein](https://github.com/chernistry/bernstein) and your work could appear here next month!
FOOTER

info "✅ Generated: $OUTFILE"
echo ""
cat "$OUTFILE"
