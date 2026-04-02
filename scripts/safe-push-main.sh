#!/usr/bin/env bash
# bernstein dev toolkit — interactive chore menu
# Usage: ./scripts/safe-push-main.sh [--push]  (--push skips the menu and just pushes)
set -euo pipefail

# ── colours & symbols ────────────────────────────────────────────────────────
RED='\033[0;31m'; YEL='\033[0;33m'; GRN='\033[0;32m'
BLU='\033[0;34m'; CYN='\033[0;36m'; MAG='\033[0;35m'
BOLD='\033[1m'; DIM='\033[2m'; RST='\033[0m'
CHECK="${GRN}✓${RST}"; CROSS="${RED}✗${RST}"; ARROW="${CYN}→${RST}"

REMOTE="${REMOTE:-origin}"
BRANCH="${BRANCH:-main}"

# ── helpers ──────────────────────────────────────────────────────────────────
_header() {
  local cols; cols=$(tput cols 2>/dev/null || echo 72)
  local line; line=$(printf '─%.0s' $(seq 1 "$cols"))
  echo -e "\n${DIM}${line}${RST}"
  echo -e "${BOLD}${BLU}  ◆ $1${RST}"
  echo -e "${DIM}${line}${RST}"
}

_ok()   { echo -e "  ${CHECK} $1"; }
_warn() { echo -e "  ${YEL}⚠${RST}  $1"; }
_err()  { echo -e "  ${CROSS} $1" >&2; }
_step() { echo -e "  ${ARROW} ${DIM}$1${RST}"; }

_branch() { git rev-parse --abbrev-ref HEAD; }
_is_dirty() { ! git diff --quiet || ! git diff --cached --quiet; }

# ── sidebar: live context ─────────────────────────────────────────────────────
_sidebar() {
  local br; br=$(_branch)
  local dirty=""; _is_dirty && dirty="${YEL} [dirty]${RST}"
  local ahead behind
  ahead=$(git rev-list --count "${REMOTE}/${BRANCH}..HEAD" 2>/dev/null || echo "?")
  behind=$(git rev-list --count "HEAD..${REMOTE}/${BRANCH}" 2>/dev/null || echo "?")
  local open_tasks; open_tasks=$(ls .sdd/backlog/open/*.yaml 2>/dev/null | wc -l | tr -d ' ')
  local ci_status=""
  if command -v gh &>/dev/null; then
    ci_status=$(gh run list --workflow ci.yml --branch main --limit 1 \
      --json conclusion --jq '.[0].conclusion // "unknown"' 2>/dev/null || echo "unknown")
    case "$ci_status" in
      success)  ci_status="${GRN}✓ passing${RST}" ;;
      failure)  ci_status="${RED}✗ failing${RST}" ;;
      *)        ci_status="${YEL}~ ${ci_status}${RST}" ;;
    esac
  fi

  echo -e "\n${DIM}┌─ project snapshot ─────────────────────────────────────────┐${RST}"
  printf "${DIM}│${RST}  branch   ${BOLD}%s${RST}%b\n" "$br" "$dirty"
  printf "${DIM}│${RST}  ahead/behind  ${GRN}+%s${RST} / ${RED}-%s${RST} commits vs %s/%s\n" \
    "$ahead" "$behind" "$REMOTE" "$BRANCH"
  [[ -n "$ci_status" ]] && printf "${DIM}│${RST}  CI        %b\n" "$ci_status"
  printf "${DIM}│${RST}  backlog   ${MAG}%s${RST} open tickets\n" "$open_tasks"
  echo -e "${DIM}└────────────────────────────────────────────────────────────┘${RST}"
}

# ═══════════════════════════════════════════════════════════════════
# Option 1 — Safe Push (fetch → rebase → push, never force)
# ═══════════════════════════════════════════════════════════════════
do_safe_push() {
  _header "Safe Push  →  ${REMOTE}/${BRANCH}"

  local br; br=$(_branch)
  if [[ "$br" != "$BRANCH" ]]; then
    _err "On branch '${br}', expected '${BRANCH}'. Aborting."
    return 1
  fi

  if _is_dirty; then
    _err "Working tree is dirty. Commit or stash first."
    return 1
  fi

  _step "fetching ${REMOTE}/${BRANCH} …"
  git fetch "${REMOTE}" "${BRANCH}"

  _step "rebasing onto ${REMOTE}/${BRANCH} …"
  git rebase "${REMOTE}/${BRANCH}"

  _step "pushing …"
  git push "${REMOTE}" "${BRANCH}"

  _ok "Pushed ${BOLD}${REMOTE}/${BRANCH}${RST} successfully."
}

# ═══════════════════════════════════════════════════════════════════
# Option 2 — Lint + Type-check + Test, then commit fixes
# ═══════════════════════════════════════════════════════════════════
do_ci_fix() {
  _header "CI Fix — lint → pyright → tests"

  _step "ruff check --fix …"
  uv run ruff check src/ --fix 2>&1 | tail -4

  _step "ruff format …"
  uv run ruff format src/ tests/ 2>&1 | tail -2

  _step "pyright …"
  if uv run pyright src/ 2>&1 | tail -6; then
    _ok "pyright clean"
  else
    _warn "pyright has errors — check output above"
  fi

  _step "pytest (isolated runner) …"
  if uv run python scripts/run_tests.py -x 2>&1 | tail -8; then
    _ok "tests pass"
  else
    _warn "test failures — check output above"
  fi

  if _is_dirty; then
    echo ""
    read -rp "  $(echo -e "${CYN}Commit auto-fixes?${RST} [Y/n] ")" yn
    yn="${yn:-Y}"
    if [[ "$yn" =~ ^[Yy]$ ]]; then
      git add -A
      git diff --cached --quiet && { _warn "nothing staged"; return; }
      git commit -m "fix(ci): auto-fix ruff, format, and lint issues"
      _ok "committed"
    fi
  else
    _ok "working tree clean — nothing to commit"
  fi
}

# ═══════════════════════════════════════════════════════════════════
# Option 3 — Clean up agent worktrees + stale branches
# ═══════════════════════════════════════════════════════════════════
do_git_clean() {
  _header "Git Hygiene — prune worktrees & stale branches"

  local wt_count; wt_count=$(git worktree list | grep -v "^$(git rev-parse --show-toplevel)" | wc -l | tr -d ' ')
  local branch_count; branch_count=$(git branch | grep -c "agent/" 2>/dev/null || echo 0)
  local stash_count; stash_count=$(git stash list | wc -l | tr -d ' ')

  echo -e "  Worktrees (non-main):  ${BOLD}${wt_count}${RST}"
  echo -e "  Agent branches:        ${BOLD}${branch_count}${RST}"
  echo -e "  Stashes:               ${BOLD}${stash_count}${RST}"
  echo ""

  if [[ "$wt_count" -eq 0 && "$branch_count" -eq 0 ]]; then
    _ok "Nothing to clean."
    return
  fi

  read -rp "  $(echo -e "${YEL}Remove all agent worktrees and branches?${RST} [y/N] ")" yn
  yn="${yn:-N}"
  [[ ! "$yn" =~ ^[Yy]$ ]] && { _warn "skipped"; return; }

  for wt in .sdd/worktrees/*/; do
    [[ -d "$wt" ]] || continue
    git worktree remove "$wt" --force 2>/dev/null \
      && _ok "worktree removed: $(basename "$wt")" \
      || _warn "could not remove: $wt"
  done

  git branch | grep "agent/" | while read -r br; do
    git branch -D "$br" 2>/dev/null \
      && _ok "branch deleted: ${br}" \
      || _warn "could not delete: ${br}"
  done

  _step "pruning remote refs …"
  git remote prune "${REMOTE}" 2>/dev/null && _ok "remote pruned"
}

# ═══════════════════════════════════════════════════════════════════
# Option 4 — Clean .sdd/runtime (reset for a fresh bernstein run)
# ═══════════════════════════════════════════════════════════════════
do_runtime_clean() {
  _header "Runtime Reset — wipe .sdd/runtime for a fresh start"

  local rt=".sdd/runtime"
  local pid_files; pid_files=$(find "$rt/pids" -name "*.json" 2>/dev/null | wc -l | tr -d ' ')
  local log_files; log_files=$(find "$rt" -name "*.log" 2>/dev/null | wc -l | tr -d ' ')
  local signal_dirs; signal_dirs=$(find "$rt/signals" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')

  echo -e "  PID files:    ${BOLD}${pid_files}${RST}"
  echo -e "  Log files:    ${BOLD}${log_files}${RST}"
  echo -e "  Signal dirs:  ${BOLD}${signal_dirs}${RST}"
  echo ""

  read -rp "  $(echo -e "${YEL}Wipe .sdd/runtime? Backlog and metrics are preserved.${RST} [y/N] ")" yn
  yn="${yn:-N}"
  [[ ! "$yn" =~ ^[Yy]$ ]] && { _warn "skipped"; return; }

  local to_remove=(
    tasks.jsonl session_state.json task_graph.json
    agents.json provider_status.json config_state.json
    session.json supervisor_state.json
  )
  for f in "${to_remove[@]}"; do
    rm -f "${rt}/${f}" && _ok "removed ${f}" 2>/dev/null || true
  done

  rm -f "${rt}"/*.log
  rm -rf "${rt}/signals" "${rt}/costs" "${rt}/incidents" \
         "${rt}/manifests" "${rt}/wal" "${rt}/gates" "${rt}/pids"
  rm -f .sdd/index/codebase.db

  _ok "Runtime clean. Start fresh with: ${BOLD}bernstein run${RST}"
}

# ═══════════════════════════════════════════════════════════════════
# Option 5 — Check CI status + open failing log in browser
# ═══════════════════════════════════════════════════════════════════
do_ci_status() {
  _header "CI Status — latest runs on ${BRANCH}"

  if ! command -v gh &>/dev/null; then
    _err "gh CLI not found — install with: brew install gh"
    return 1
  fi

  _step "fetching run list …"
  local runs
  runs=$(gh run list --workflow ci.yml --branch "${BRANCH}" --limit 5 \
    --json databaseId,status,conclusion,displayTitle,createdAt \
    --jq '.[] | "\(.databaseId)\t\(.status)\t\(.conclusion // "in_progress")\t\(.displayTitle[0:55])\t\(.createdAt[0:16])"' \
    2>/dev/null)

  if [[ -z "$runs" ]]; then
    _warn "No runs found for workflow ci.yml on ${BRANCH}"
    return
  fi

  echo ""
  printf "  ${BOLD}%-14s %-12s %-12s %-57s %s${RST}\n" "Run ID" "Status" "Result" "Title" "Date"
  echo -e "  ${DIM}$(printf '─%.0s' $(seq 1 110))${RST}"
  while IFS=$'\t' read -r id status conclusion title date; do
    local color="$RST"
    local icon="·"
    case "$conclusion" in
      success)     color="$GRN"; icon="✓" ;;
      failure)     color="$RED"; icon="✗" ;;
      in_progress) color="$YEL"; icon="↻" ;;
      cancelled)   color="$DIM"; icon="⊘" ;;
    esac
    printf "  ${color}${icon}${RST} %-12s ${color}%-12s %-12s${RST} %-57s %s\n" \
      "$id" "$status" "$conclusion" "$title" "$date"
  done <<< "$runs"

  echo ""
  local latest_id; latest_id=$(echo "$runs" | head -1 | cut -f1)
  local latest_conclusion; latest_conclusion=$(echo "$runs" | head -1 | cut -f3)

  if [[ "$latest_conclusion" == "failure" ]]; then
    read -rp "  $(echo -e "${RED}Latest run failed.${RST} Open in browser? [Y/n] ")" yn
    yn="${yn:-Y}"
    [[ "$yn" =~ ^[Yy]$ ]] && gh run view "${latest_id}" --web
  fi
}

# ═══════════════════════════════════════════════════════════════════
# Main menu
# ═══════════════════════════════════════════════════════════════════
main_menu() {
  while true; do
    clear
    echo -e "\n${BOLD}${BLU}  ◆ bernstein dev toolkit${RST}${DIM}  (chernistry/bernstein)${RST}"
    _sidebar

    echo -e "\n  ${BOLD}Choose an action:${RST}\n"
    echo -e "  ${BOLD}${CYN}1${RST}  ${BOLD}Safe Push${RST}            fetch → rebase → push  ${DIM}(never force)${RST}"
    echo -e "  ${BOLD}${CYN}2${RST}  ${BOLD}CI Fix & Commit${RST}      ruff + pyright + tests → auto-commit fixes"
    echo -e "  ${BOLD}${CYN}3${RST}  ${BOLD}Git Clean${RST}            prune agent worktrees & stale branches"
    echo -e "  ${BOLD}${CYN}4${RST}  ${BOLD}Runtime Reset${RST}        wipe .sdd/runtime for a fresh bernstein run"
    echo -e "  ${BOLD}${CYN}5${RST}  ${BOLD}CI Status${RST}            view latest workflow runs, open failures in browser"
    echo -e "  ${BOLD}${CYN}q${RST}  ${DIM}Quit${RST}"
    echo ""
    read -rp "  $(echo -e "${CYN}→ ${RST}")" choice

    case "$choice" in
      1) do_safe_push  && echo -e "\n  ${GRN}Done.${RST}" || echo -e "\n  ${RED}Failed.${RST}" ;;
      2) do_ci_fix     && echo -e "\n  ${GRN}Done.${RST}" || echo -e "\n  ${RED}Failed.${RST}" ;;
      3) do_git_clean  && echo -e "\n  ${GRN}Done.${RST}" || echo -e "\n  ${RED}Failed.${RST}" ;;
      4) do_runtime_clean && echo -e "\n  ${GRN}Done.${RST}" || echo -e "\n  ${RED}Failed.${RST}" ;;
      5) do_ci_status  && echo -e "\n  ${GRN}Done.${RST}" || echo -e "\n  ${RED}Failed.${RST}" ;;
      q|Q|"") echo -e "  ${DIM}bye.${RST}\n"; exit 0 ;;
      *) echo -e "  ${YEL}Unknown option '${choice}'.${RST}" ;;
    esac

    echo ""
    read -rp "  $(echo -e "${DIM}press enter to return to menu…${RST}")" _
  done
}

# ── entry point ───────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--push" ]]; then
  do_safe_push
else
  main_menu
fi
