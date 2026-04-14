#!/bin/sh
set -e

# Detect OS and architecture
OS=$(uname -s | tr '[:upper:]' '[:lower:]')
ARCH=$(uname -m)

echo "Installing Bernstein on $OS/$ARCH..."

# Check for Python 3.12+
if ! command -v python3 >/dev/null 2>&1; then
  echo "Error: Python 3.12+ required. Install from python.org"
  exit 1
fi

PYTHON_VERSION=$(python3 --version | awk '{print $2}')
if [ "$(printf '%s\n' "3.12" "$PYTHON_VERSION" | sort -V | head -n1)" != "3.12" ]; then
  echo "Error: Python 3.12+ required. Current version: $PYTHON_VERSION"
  exit 1
fi

# Install pipx if not present
if ! command -v pipx >/dev/null 2>&1; then
  echo "Installing pipx..."
  python3 -m pip install --user pipx
  python3 -m pipx ensurepath
  export PATH="$PATH:~/.local/bin"
fi

# Install Bernstein
pipx install bernstein

echo ""
echo "Bernstein installed! Run: bernstein --version"
echo "Get started: bernstein -g 'your goal here'"