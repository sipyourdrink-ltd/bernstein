#!/usr/bin/env bash
# Multi-model demo: run the same project with 4 different CLI agents.
#
# This script shows the key differentiator of Bernstein vs every other
# agent framework: model-agnostic orchestration. Same task graph, same
# file-based state, same verification — different CLI agents doing the work.
#
# Prerequisites: bernstein installed (pipx install bernstein or uv tool install bernstein)
# At least one CLI agent must be installed. The script detects what's available.

set -euo pipefail

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║  Bernstein — Multi-model demo                        ║"
echo "║  Claude · Codex · Gemini · Qwen in a single run     ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""

# Check which agents are available
AGENTS=()

if command -v claude &>/dev/null; then
    AGENTS+=("claude (Claude Code / Anthropic)")
fi
if command -v codex &>/dev/null; then
    AGENTS+=("codex (Codex CLI / OpenAI)")
fi
if command -v gemini &>/dev/null; then
    AGENTS+=("gemini (Gemini CLI / Google)")
fi
if command -v qwen &>/dev/null; then
    AGENTS+=("qwen (Qwen / Alibaba / OpenRouter)")
fi

if [ ${#AGENTS[@]} -eq 0 ]; then
    echo "No supported CLI agents found. Install at least one:"
    echo "  npm install -g @anthropic-ai/claude-code   # Claude Code"
    echo "  npm install -g @openai/codex               # Codex"
    echo "  npm install -g @google/gemini-cli          # Gemini"
    echo "  npm install -g qwen-code                   # Qwen"
    exit 1
fi

echo "Detected agents:"
for a in "${AGENTS[@]}"; do
    echo "  ✓ $a"
done
echo ""

# Demo mode: run a fresh temp project using this directory's bernstein.yaml
DEMO_DIR="$(mktemp -d)/multi-model-demo"
mkdir -p "$DEMO_DIR"
cp "$(dirname "$0")/bernstein.yaml" "$DEMO_DIR/"

echo "Working directory: $DEMO_DIR"
echo ""
echo "Starting bernstein..."
echo ""

cd "$DEMO_DIR"
bernstein run --budget 2.00

echo ""
echo "Done. Check $DEMO_DIR for the generated code."
echo "Task log: $DEMO_DIR/.sdd/runtime/"
echo ""
echo "Cost summary:"
bernstein cost 2>/dev/null || true
