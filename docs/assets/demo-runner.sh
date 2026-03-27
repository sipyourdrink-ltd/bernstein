#!/usr/bin/env bash
# Bernstein demo runner — simulates a real orchestration run for recording.
# Used by demo.tape (vhs) to produce docs/assets/demo.gif.
# Run directly to preview: bash docs/assets/demo-runner.sh

set -euo pipefail

RESET='\033[0m'
BOLD='\033[1m'
DIM='\033[2m'
RED='\033[31m'
GREEN='\033[32m'
YELLOW='\033[33m'
BLUE='\033[34m'
CYAN='\033[36m'
WHITE='\033[97m'

sleep_short() { sleep "${1:-0.3}"; }

# ── Banner ────────────────────────────────────────────────────────────────────
echo ""
printf "${BLUE}${BOLD}"
echo "╔══════════════════════════════════╗"
echo "║  🎼 Bernstein — Agent Orchestra  ║"
echo "╚══════════════════════════════════╝"
printf "${RESET}\n"
sleep_short 0.4

# ── Goal parsing ──────────────────────────────────────────────────────────────
printf "${DIM}Goal:${RESET} ${WHITE}\"Add auth, tests, and docs\"${RESET}\n"
sleep_short 0.3
printf "${DIM}Decomposing into tasks...${RESET}\n"
sleep_short 0.6

# ── Task plan ─────────────────────────────────────────────────────────────────
printf "\n${BOLD}${WHITE}Tasks created:${RESET}\n"
sleep_short 0.2
printf "  ${CYAN}[T-001]${RESET} ${WHITE}Implement JWT authentication middleware${RESET}   ${DIM}role=backend  effort=medium${RESET}\n"
sleep_short 0.15
printf "  ${CYAN}[T-002]${RESET} ${WHITE}Write unit + integration tests for auth${RESET}   ${DIM}role=qa       effort=low${RESET}\n"
sleep_short 0.15
printf "  ${CYAN}[T-003]${RESET} ${WHITE}Generate API docs with usage examples${RESET}    ${DIM}role=docs     effort=low${RESET}\n"
sleep_short 0.4

# ── Agent spawning ────────────────────────────────────────────────────────────
printf "\n${BOLD}${WHITE}Spawning agents...${RESET}\n"
sleep_short 0.3

printf "  ${GREEN}▶${RESET} ${WHITE}claude-backend${RESET}  ${DIM}[claude-sonnet-4-6]${RESET}  claimed T-001\n"
sleep_short 0.5
printf "  ${GREEN}▶${RESET} ${WHITE}claude-qa${RESET}       ${DIM}[claude-haiku-4-5]${RESET}   claimed T-002\n"
sleep_short 0.4
printf "  ${GREEN}▶${RESET} ${WHITE}claude-docs${RESET}     ${DIM}[claude-haiku-4-5]${RESET}   claimed T-003\n"
sleep_short 0.4

# ── Live activity feed ────────────────────────────────────────────────────────
printf "\n${DIM}───────────────────────────────────────────────────────────${RESET}\n"
sleep_short 0.2

printf "${DIM}[00:04]${RESET} ${CYAN}backend${RESET}  Creating src/auth/jwt.py...\n"
sleep_short 0.35
printf "${DIM}[00:08]${RESET} ${CYAN}backend${RESET}  Adding middleware to FastAPI app...\n"
sleep_short 0.4
printf "${DIM}[00:11]${RESET} ${CYAN}docs${RESET}     Scanning existing routes...\n"
sleep_short 0.3
printf "${DIM}[00:14]${RESET} ${CYAN}qa${RESET}       Writing test_auth.py (12 test cases)...\n"
sleep_short 0.5
printf "${DIM}[00:19]${RESET} ${CYAN}backend${RESET}  Adding refresh token endpoint...\n"
sleep_short 0.4
printf "${DIM}[00:23]${RESET} ${CYAN}docs${RESET}     Writing docs/api/auth.md...\n"
sleep_short 0.3
printf "${DIM}[00:27]${RESET} ${CYAN}qa${RESET}       Running pytest... ${GREEN}12 passed${RESET}\n"
sleep_short 0.5
printf "${DIM}[00:31]${RESET} ${CYAN}backend${RESET}  Committing: feat(auth): add JWT middleware\n"
sleep_short 0.4

# ── Janitor verification ──────────────────────────────────────────────────────
printf "\n${BOLD}${YELLOW}Verifying results...${RESET}\n"
sleep_short 0.3
printf "  ${GREEN}✓${RESET} Tests pass   ${DIM}(12/12)${RESET}\n"
sleep_short 0.2
printf "  ${GREEN}✓${RESET} No regressions  ${DIM}(124 existing tests still pass)${RESET}\n"
sleep_short 0.2
printf "  ${GREEN}✓${RESET} Files committed  ${DIM}(src/auth/jwt.py, tests/test_auth.py, docs/api/auth.md)${RESET}\n"
sleep_short 0.4

# ── Completion summary ────────────────────────────────────────────────────────
printf "\n${BLUE}${BOLD}"
echo "╔═══════════════════════════════════════╗"
printf "║  ${GREEN}✓ 3 tasks done${BLUE}   \$0.42 spent   47s  ║\n"
echo "╚═══════════════════════════════════════╝"
printf "${RESET}\n"
sleep_short 0.5

printf "${DIM}git log --oneline -3:${RESET}\n"
sleep_short 0.2
printf "  ${GREEN}a3f9c1b${RESET} feat(auth): add JWT middleware\n"
sleep_short 0.15
printf "  ${GREEN}b8e2d44${RESET} test(auth): 12 unit + integration tests\n"
sleep_short 0.15
printf "  ${GREEN}c1a5e7f${RESET} docs(api): auth endpoint reference\n"
sleep_short 0.5
