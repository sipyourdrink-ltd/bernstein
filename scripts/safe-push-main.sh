#!/usr/bin/env bash
# bernstein dev toolkit — interactive chore menu with CI monitoring
# Usage:
#   ./scripts/safe-push-main.sh          # interactive menu
#   ./scripts/safe-push-main.sh --push   # validate → push → monitor CI → verify release
#   ./scripts/safe-push-main.sh --ship   # full pipeline: lint/fix → push → monitor → release check
set -euo pipefail

# ── colours & symbols ────────────────────────────────────────────────────────
RED='\033[0;31m'; YEL='\033[0;33m'; GRN='\033[0;32m'
BLU='\033[0;34m'; CYN='\033[0;36m'; MAG='\033[0;35m'
BOLD='\033[1m'; DIM='\033[2m'; RST='\033[0m'
CHECK="${GRN}✓${RST}"; CROSS="${RED}✗${RST}"; ARROW="${CYN}→${RST}"

# Constants for repeated string literals
readonly STATUS_IN_PROGRESS="in_progress"

REMOTE="${REMOTE:-origin}"
BRANCH="${BRANCH:-main}"
CI_WORKFLOW="${CI_WORKFLOW:-ci.yml}"
RELEASE_WORKFLOW="${RELEASE_WORKFLOW:-Auto-release}"
CI_POLL_INTERVAL="${CI_POLL_INTERVAL:-30}"
CI_TIMEOUT="${CI_TIMEOUT:-900}"

# ── helpers ──────────────────────────────────────────────────────────────────
_header() {
  local title="$1"
  local cols; cols=$(tput cols 2>/dev/null || echo 72)
  local line; line=$(printf '─%.0s' $(seq 1 "$cols"))
  echo -e "\n${DIM}${line}${RST}"
  echo -e "${BOLD}${BLU}  ◆ ${title}${RST}"
  echo -e "${DIM}${line}${RST}"
  return 0
}

_ok()   { local msg="$1"; echo -e "  ${CHECK} ${msg}"; return 0; }
_warn() { local msg="$1"; echo -e "  ${YEL}⚠${RST}  ${msg}"; return 0; }
_err()  { local msg="$1"; echo -e "  ${CROSS} ${msg}" >&2; return 0; }
_step() { local msg="$1"; echo -e "  ${ARROW} ${DIM}${msg}${RST}"; return 0; }

_branch() { git rev-parse --abbrev-ref HEAD; return 0; }
_is_dirty() { ! git diff --quiet || ! git diff --cached --quiet; return $?; }

_require_gh() {
  if ! command -v gh &>/dev/null; then
    _err "gh CLI not found — install with: brew install gh"
    return 1
  fi
}

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
    ci_status=$(gh run list --workflow "$CI_WORKFLOW" --branch "$BRANCH" --limit 1 \
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
  return 0
}

# ═══════════════════════════════════════════════════════════════════
# Local validation — runs the same checks as CI
# ═══════════════════════════════════════════════════════════════════
do_local_validate() {
  _header "Local Validation — ruff, pyright, tests"
  local failed=0

  _step "ruff check …"
  if ! uv run ruff check src/ 2>&1 | tail -3; then
    _err "ruff check failed"
    failed=1
  else
    _ok "ruff check"
  fi

  _step "ruff format --check …"
  if ! uv run ruff format --check src/ tests/ 2>&1 | tail -3; then
    _err "ruff format needs fixing"
    failed=1
  else
    _ok "ruff format"
  fi

  _step "pyright …"
  if ! uv run pyright src/ 2>&1 | tail -3; then
    _err "pyright errors"
    failed=1
  else
    _ok "pyright"
  fi

  _step "checking .sdd not tracked …"
  local tracked; tracked=$(git ls-files '.sdd' 2>/dev/null)
  if [[ -n "$tracked" ]]; then
    _err ".sdd files tracked in git (CI will fail):"
    printf '%s\n' "$tracked" | head -5
    failed=1
  else
    _ok ".sdd not tracked"
  fi

  _step "tests (isolated runner) …"
  if ! uv run python scripts/run_tests.py -x 2>&1 | tail -8; then
    _err "tests failed"
    failed=1
  else
    _ok "tests pass"
  fi

  return "$failed"
}

# ═══════════════════════════════════════════════════════════════════
# CI Fix — auto-format and fix lint issues
# ═══════════════════════════════════════════════════════════════════
do_ci_fix() {
  _header "CI Fix — lint → format → pyright"

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
# Safe Push — fetch → rebase → push (never force)
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

  local behind; behind=$(git rev-list --count "HEAD..${REMOTE}/${BRANCH}" 2>/dev/null)
  if [[ "$behind" -gt 0 ]]; then
    _step "rebasing onto ${REMOTE}/${BRANCH} ($behind commits behind) …"
    git rebase "${REMOTE}/${BRANCH}"
  else
    _ok "already up to date with ${REMOTE}/${BRANCH}"
  fi

  local ahead; ahead=$(git rev-list --count "${REMOTE}/${BRANCH}..HEAD" 2>/dev/null)
  if [[ "$ahead" -eq 0 ]]; then
    _warn "nothing to push"
    return 0
  fi

  _step "pushing $ahead commit(s) …"
  git push "${REMOTE}" "${BRANCH}"
  _ok "Pushed ${BOLD}${REMOTE}/${BRANCH}${RST} successfully."
}

# ═══════════════════════════════════════════════════════════════════
# Monitor CI — poll GitHub Actions until CI finishes
# ═══════════════════════════════════════════════════════════════════
do_monitor_ci() {
  _header "Monitoring CI  →  ${CI_WORKFLOW} on ${BRANCH}"
  _require_gh || return 1

  local head_sha; head_sha=$(git rev-parse HEAD)
  local short_sha; short_sha=$(git rev-parse --short HEAD)
  _step "waiting for CI to start for ${short_sha} …"

  local run_id=""
  local waited=0
  while [[ -z "$run_id" ]] && [[ "$waited" -lt 120 ]]; do
    run_id=$(gh run list --workflow "$CI_WORKFLOW" --branch "$BRANCH" --limit 5 \
      --json databaseId,headSha,status \
      --jq ".[] | select(.headSha == \"${head_sha}\") | .databaseId" 2>/dev/null | head -1)
    if [[ -z "$run_id" ]]; then
      sleep 5
      waited=$((waited + 5))
    fi
  done

  if [[ -z "$run_id" ]]; then
    _err "CI did not start within 120s for commit ${short_sha}"
    return 1
  fi

  _ok "CI run ${run_id} started"

  local elapsed=0
  local status="${STATUS_IN_PROGRESS}"
  local conclusion=""
  local spinner=('⠋' '⠙' '⠹' '⠸' '⠼' '⠴' '⠦' '⠧' '⠇' '⠏')
  local spin_idx=0

  while [[ "$status" == "${STATUS_IN_PROGRESS}" || "$status" == "queued" || "$status" == "waiting" ]]; do
    if [[ "$elapsed" -ge "$CI_TIMEOUT" ]]; then
      _err "CI timed out after ${CI_TIMEOUT}s"
      return 1
    fi

    local s="${spinner[$spin_idx]}"
    spin_idx=$(( (spin_idx + 1) % ${#spinner[@]} ))
    printf "\r  ${CYN}${s}${RST}  CI running … ${DIM}%ds elapsed${RST}   " "$elapsed"

    sleep "$CI_POLL_INTERVAL"
    elapsed=$((elapsed + CI_POLL_INTERVAL))

    local run_data
    run_data=$(gh run view "$run_id" --json status,conclusion 2>/dev/null)
    status=$(echo "$run_data" | jq -r '.status // "unknown"')
    conclusion=$(echo "$run_data" | jq -r '.conclusion // ""')
  done

  echo ""

  if [[ "$conclusion" == "success" ]]; then
    _ok "CI passed in ~${elapsed}s ${GRN}✓${RST}"
    return 0
  else
    _err "CI failed (conclusion: ${conclusion})"
    _step "opening failure log …"
    gh run view "$run_id" --log-failed 2>/dev/null | tail -30
    echo ""
    read -rp "  $(echo -e "${RED}Open in browser?${RST} [Y/n] ")" yn
    yn="${yn:-Y}"
    [[ "$yn" =~ ^[Yy]$ ]] && gh run view "$run_id" --web
    return 1
  fi
}

# ═══════════════════════════════════════════════════════════════════
# Monitor Auto-release — verify autorelease triggered and succeeded
# ═══════════════════════════════════════════════════════════════════
do_monitor_release() {
  _header "Monitoring Auto-release"
  _require_gh || return 1

  _step "waiting for auto-release workflow to start …"

  local release_run_id=""
  local waited=0
  while [[ -z "$release_run_id" ]] && [[ "$waited" -lt 120 ]]; do
    release_run_id=$(gh run list --workflow "auto-release.yml" --branch "$BRANCH" --limit 3 \
      --json databaseId,status,createdAt \
      --jq 'sort_by(.createdAt) | reverse | .[0].databaseId // empty' 2>/dev/null)

    local run_status
    run_status=$(gh run list --workflow "auto-release.yml" --branch "$BRANCH" --limit 1 \
      --json status --jq '.[0].status // "unknown"' 2>/dev/null)

    if [[ "$run_status" == "completed" ]] && [[ "$waited" -gt 10 ]]; then
      break
    fi
    if [[ -n "$release_run_id" ]] && [[ "$run_status" == "${STATUS_IN_PROGRESS}" || "$run_status" == "queued" ]]; then
      break
    fi
    sleep 5
    waited=$((waited + 5))
  done

  if [[ -z "$release_run_id" ]]; then
    _warn "auto-release did not trigger (may be skipped if tag exists)"
    return 0
  fi

  _ok "auto-release run ${release_run_id} detected"

  local elapsed=0
  local status="${STATUS_IN_PROGRESS}"
  local conclusion=""
  while [[ "$status" == "${STATUS_IN_PROGRESS}" || "$status" == "queued" || "$status" == "waiting" ]]; do
    if [[ "$elapsed" -ge 300 ]]; then
      _err "auto-release timed out after 300s"
      return 1
    fi
    printf "\r  ${CYN}↻${RST}  auto-release running … ${DIM}%ds${RST}   " "$elapsed"
    sleep 10
    elapsed=$((elapsed + 10))
    local run_data
    run_data=$(gh run view "$release_run_id" --json status,conclusion 2>/dev/null)
    status=$(echo "$run_data" | jq -r '.status // "unknown"')
    conclusion=$(echo "$run_data" | jq -r '.conclusion // ""')
  done

  echo ""

  if [[ "$conclusion" == "success" ]]; then
    local latest_tag; latest_tag=$(git tag --sort=-v:refname | head -1)
    local latest_release; latest_release=$(gh release list --limit 1 --json tagName --jq '.[0].tagName' 2>/dev/null)
    _ok "Auto-release succeeded"
    _ok "Latest tag:     ${BOLD}${latest_tag}${RST}"
    _ok "Latest release: ${BOLD}${latest_release}${RST}"
    _ok "PyPI:           ${BOLD}https://pypi.org/project/bernstein/${RST}"
    return 0
  elif [[ "$conclusion" == "skipped" ]]; then
    _warn "auto-release was skipped (CI may not have passed)"
    return 1
  else
    _err "auto-release failed (conclusion: ${conclusion})"
    gh run view "$release_run_id" --log-failed 2>/dev/null | tail -20
    return 1
  fi
}

# ═══════════════════════════════════════════════════════════════════
# Git Hygiene — prune worktrees, branches, stale refs
# ═══════════════════════════════════════════════════════════════════
do_git_clean() {
  _header "Git Hygiene — prune worktrees & stale branches"

  local wt_count; wt_count=$(git worktree list | grep -v "^$(git rev-parse --show-toplevel)" | wc -l | tr -d ' ')
  local agent_branches; agent_branches=$(git branch | grep -E "agent/|codex/" 2>/dev/null | wc -l | tr -d ' ')
  local stash_count; stash_count=$(git stash list | wc -l | tr -d ' ')

  echo -e "  Worktrees (non-main):  ${BOLD}${wt_count}${RST}"
  echo -e "  Agent branches:        ${BOLD}${agent_branches}${RST}"
  echo -e "  Stashes:               ${BOLD}${stash_count}${RST}"
  echo ""

  if [[ "$wt_count" -eq 0 && "$agent_branches" -eq 0 ]]; then
    _ok "Nothing to clean."
  else
    read -rp "  $(echo -e "${YEL}Remove all agent worktrees and branches?${RST} [y/N] ")" yn
    yn="${yn:-N}"
    if [[ "$yn" =~ ^[Yy]$ ]]; then
      for wt in .sdd/worktrees/*/; do
        [[ -d "$wt" ]] || continue
        git worktree remove "$wt" --force 2>/dev/null \
          && _ok "worktree removed: $(basename "$wt")" \
          || _warn "could not remove: $wt"
      done
      git branch | grep -E "agent/|codex/" | while read -r br; do
        git branch -D "$br" 2>/dev/null \
          && _ok "branch deleted: ${br}" \
          || _warn "could not delete: ${br}"
      done
    fi
  fi

  _step "pruning remote refs …"
  git remote prune "${REMOTE}" 2>/dev/null && _ok "remote pruned"
  return 0
}

# ═══════════════════════════════════════════════════════════════════
# Runtime Reset — wipe .sdd/runtime
# ═══════════════════════════════════════════════════════════════════
do_runtime_clean() {
  _header "Runtime Reset — wipe .sdd/runtime for a fresh start"

  local rt=".sdd/runtime"
  local pid_files; pid_files=$(find "$rt/pids" -name "*.json" 2>/dev/null | wc -l | tr -d ' ')
  local log_files; log_files=$(find "$rt" -name "*.log" 2>/dev/null | wc -l | tr -d ' ')

  echo -e "  PID files:    ${BOLD}${pid_files}${RST}"
  echo -e "  Log files:    ${BOLD}${log_files}${RST}"
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
    rm -f "${rt}/${f}" 2>/dev/null || true
  done

  rm -f "${rt}"/*.log
  rm -rf "${rt}/signals" "${rt}/costs" "${rt}/incidents" \
         "${rt}/manifests" "${rt}/wal" "${rt}/gates" "${rt}/pids"
  rm -f .sdd/index/codebase.db

  _ok "Runtime clean. Start fresh with: ${BOLD}bernstein run${RST}"
}

# ═══════════════════════════════════════════════════════════════════
# CI Status — show recent runs
# ═══════════════════════════════════════════════════════════════════
do_ci_status() {
  _header "CI Status — latest runs on ${BRANCH}"
  _require_gh || return 1

  _step "fetching run list …"
  local runs
  runs=$(gh run list --workflow "$CI_WORKFLOW" --branch "${BRANCH}" --limit 5 \
    --json databaseId,status,conclusion,displayTitle,createdAt \
    --jq '.[] | "\(.databaseId)\t\(.status)\t\(.conclusion // "in_progress")\t\(.displayTitle[0:55])\t\(.createdAt[0:16])"' \
    2>/dev/null)

  if [[ -z "$runs" ]]; then
    _warn "No runs found for workflow ${CI_WORKFLOW} on ${BRANCH}"
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
      *)           color="$DIM"; icon="?" ;;
    esac
    printf "  ${color}${icon}${RST} %-12s ${color}%-12s %-12s${RST} %-57s %s\n" \
      "$id" "$status" "$conclusion" "$title" "$date"
  done <<< "$runs"

  echo ""
  local latest_conclusion; latest_conclusion=$(echo "$runs" | head -1 | cut -f3)
  if [[ "$latest_conclusion" == "failure" ]]; then
    local latest_id; latest_id=$(echo "$runs" | head -1 | cut -f1)
    read -rp "  $(echo -e "${RED}Latest run failed.${RST} Open in browser? [Y/n] ")" yn
    yn="${yn:-Y}"
    [[ "$yn" =~ ^[Yy]$ ]] && gh run view "${latest_id}" --web
  fi
}

# ═══════════════════════════════════════════════════════════════════
# Ship — the full pipeline: validate → push → CI → release
# ═══════════════════════════════════════════════════════════════════
do_ship() {
  _header "Ship Pipeline — validate → push → CI → release"

  echo -e "  ${BOLD}Phase 1/4${RST}  Local validation"
  if ! do_local_validate; then
    _err "Local validation failed. Fix issues first."
    return 1
  fi
  echo ""

  echo -e "  ${BOLD}Phase 2/4${RST}  Push to ${REMOTE}/${BRANCH}"
  if ! do_safe_push; then
    _err "Push failed."
    return 1
  fi
  echo ""

  echo -e "  ${BOLD}Phase 3/4${RST}  Monitor CI"
  if ! do_monitor_ci; then
    _err "CI failed. Fix and retry."
    return 1
  fi
  echo ""

  echo -e "  ${BOLD}Phase 4/4${RST}  Verify auto-release"
  git fetch --tags origin 2>/dev/null
  if ! do_monitor_release; then
    _err "Auto-release did not succeed."
    return 1
  fi

  echo ""
  echo -e "  ${GRN}${BOLD}═══════════════════════════════════════════════════${RST}"
  echo -e "  ${GRN}${BOLD}  ✓  SHIP COMPLETE  ✓${RST}"
  echo -e "  ${GRN}${BOLD}═══════════════════════════════════════════════════${RST}"
  echo ""
}

# ═══════════════════════════════════════════════════════════════════
# Merge all branches — merge feature branches into main
# ═══════════════════════════════════════════════════════════════════
do_merge_all() {
  _header "Merge All Branches → ${BRANCH}"

  local br; br=$(_branch)
  if [[ "$br" != "$BRANCH" ]]; then
    _err "Must be on ${BRANCH} to merge. Currently on ${br}."
    return 1
  fi

  local feature_branches
  feature_branches=$(git branch | grep -vE "^\*|main$" | sed 's/^[ *]*//' | tr -d ' ')

  if [[ -z "$feature_branches" ]]; then
    _ok "No feature branches to merge."
    return 0
  fi

  echo -e "  Feature branches found:"
  echo "$feature_branches" | while read -r fb; do
    local count; count=$(git rev-list --count "HEAD..${fb}" 2>/dev/null || echo "?")
    echo -e "    ${CYN}${fb}${RST}  (${count} commits ahead)"
  done
  echo ""

  read -rp "  $(echo -e "${YEL}Merge all into ${BRANCH}?${RST} [y/N] ")" yn
  yn="${yn:-N}"
  [[ ! "$yn" =~ ^[Yy]$ ]] && { _warn "skipped"; return; }

  echo "$feature_branches" | while read -r fb; do
    _step "merging ${fb} …"
    if git merge "$fb" --no-ff -m "merge: ${fb} into ${BRANCH}" 2>/dev/null; then
      _ok "merged: ${fb}"
      git branch -d "$fb" 2>/dev/null || true
    else
      _err "conflict merging ${fb} — aborting this merge"
      git merge --abort 2>/dev/null || true
    fi
  done
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
    echo -e "  ${BOLD}${CYN}2${RST}  ${BOLD}CI Fix & Commit${RST}      ruff + pyright → auto-commit fixes"
    echo -e "  ${BOLD}${CYN}3${RST}  ${BOLD}Local Validate${RST}       run full CI checks locally before push"
    echo -e "  ${BOLD}${CYN}4${RST}  ${BOLD}Monitor CI${RST}           watch current CI run until completion"
    echo -e "  ${BOLD}${CYN}5${RST}  ${BOLD}CI Status${RST}            view latest workflow runs, open failures"
    echo -e "  ${BOLD}${CYN}6${RST}  ${BOLD}Merge All${RST}            merge all feature branches into ${BRANCH}"
    echo -e "  ${BOLD}${CYN}7${RST}  ${BOLD}Git Clean${RST}            prune agent worktrees & stale branches"
    echo -e "  ${BOLD}${CYN}8${RST}  ${BOLD}Runtime Reset${RST}        wipe .sdd/runtime for a fresh bernstein run"
    echo -e "  ${BOLD}${CYN}9${RST}  ${BOLD}Verify Release${RST}       check if latest auto-release succeeded"
    echo -e ""
    echo -e "  ${BOLD}${GRN}s${RST}  ${BOLD}${GRN}SHIP${RST}                 ${GRN}validate → push → CI → release (full pipeline)${RST}"
    echo -e "  ${BOLD}${CYN}q${RST}  ${DIM}Quit${RST}"
    echo ""
    read -rp "  $(echo -e "${CYN}→ ${RST}")" choice

    case "$choice" in
      1) do_safe_push       && echo -e "\n  ${GRN}Done.${RST}" || echo -e "\n  ${RED}Failed.${RST}" ;;
      2) do_ci_fix          && echo -e "\n  ${GRN}Done.${RST}" || echo -e "\n  ${RED}Failed.${RST}" ;;
      3) do_local_validate  && echo -e "\n  ${GRN}Done.${RST}" || echo -e "\n  ${RED}Failed.${RST}" ;;
      4) do_monitor_ci      && echo -e "\n  ${GRN}Done.${RST}" || echo -e "\n  ${RED}Failed.${RST}" ;;
      5) do_ci_status       && echo -e "\n  ${GRN}Done.${RST}" || echo -e "\n  ${RED}Failed.${RST}" ;;
      6) do_merge_all       && echo -e "\n  ${GRN}Done.${RST}" || echo -e "\n  ${RED}Failed.${RST}" ;;
      7) do_git_clean       && echo -e "\n  ${GRN}Done.${RST}" || echo -e "\n  ${RED}Failed.${RST}" ;;
      8) do_runtime_clean   && echo -e "\n  ${GRN}Done.${RST}" || echo -e "\n  ${RED}Failed.${RST}" ;;
      9) do_monitor_release && echo -e "\n  ${GRN}Done.${RST}" || echo -e "\n  ${RED}Failed.${RST}" ;;
      s|S) do_ship          && echo -e "\n  ${GRN}Done.${RST}" || echo -e "\n  ${RED}Failed.${RST}" ;;
      q|Q|"") echo -e "  ${DIM}bye.${RST}\n"; exit 0 ;;
      *) echo -e "  ${YEL}Unknown option '${choice}'.${RST}" ;;
    esac

    echo ""
    read -rp "  $(echo -e "${DIM}press enter to return to menu…${RST}")" _
  done
  return 0
}

# ── entry point ───────────────────────────────────────────────────────────────
case "${1:-}" in
  --push)  do_safe_push && do_monitor_ci && do_monitor_release ;;
  --ship)  do_ship ;;
  --ci)    do_monitor_ci ;;
  --status) do_ci_status ;;
  --validate) do_local_validate ;;
  --release) do_monitor_release ;;
  *)       main_menu ;;
esac
