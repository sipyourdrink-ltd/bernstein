#!/bin/sh
set -eu

# Detect OS and architecture
OS=$(uname -s | tr '[:upper:]' '[:lower:]')
ARCH=$(uname -m)

echo "Installing Bernstein on $OS/$ARCH..."

# Check for Python 3.12+
if ! command -v python3 >/dev/null 2>&1; then
  echo "Error: Python 3.12+ required. Install from https://www.python.org/" >&2
  exit 1
fi

PYTHON_BIN="python3"
if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' >/dev/null 2>&1; then
  PYTHON_VERSION=$("$PYTHON_BIN" -c 'import sys; print(".".join(str(part) for part in sys.version_info[:3]))')
  echo "Error: Python 3.12+ required. Current version: $PYTHON_VERSION" >&2
  exit 1
fi

if ! "$PYTHON_BIN" -m pip --version >/dev/null 2>&1; then
  "$PYTHON_BIN" -m ensurepip --upgrade >/dev/null 2>&1 || true
fi

# Install pipx if not present
if ! command -v pipx >/dev/null 2>&1 && ! "$PYTHON_BIN" -m pipx --version >/dev/null 2>&1; then
  echo "pipx not found. Installing..."
  "$PYTHON_BIN" -m pip install --user --upgrade pipx
fi

# Ensure pipx/bernstein bin paths are usable in THIS shell
USER_BIN=$("$PYTHON_BIN" -c 'import os, site; print(os.path.join(site.USER_BASE, "bin"))')
export PATH="$USER_BIN:$HOME/.local/bin:$PATH"

run_pipx() {
  if command -v pipx >/dev/null 2>&1; then
    pipx "$@"
    return $?
  else
    "$PYTHON_BIN" -m pipx "$@"
    return $?
  fi
}

# Also ensure pipx paths are configured
run_pipx ensurepath >/dev/null 2>&1 || true

# Verify pipx works
if ! run_pipx --version >/dev/null 2>&1; then
  echo "Error: pipx is installed but not available in PATH." >&2
  echo "Try restarting your terminal or running: python3 -m pipx ensurepath" >&2
  exit 1
fi

# Install Bernstein
echo "Installing Bernstein..."
if ! run_pipx install bernstein; then
  run_pipx upgrade bernstein
fi

echo ""
echo "Bernstein installed successfully!"
echo ""
echo "Try:"
echo "  bernstein --version"
echo "  bernstein -g 'your goal here'"
