#!/usr/bin/env sh
set -e

# Detect OS and architecture
OS=$(uname -s | tr '[:upper:]' '[:lower:]')
ARCH=$(uname -m)

echo "Installing Bernstein on $OS/$ARCH..."

# Check for Python 3.12+
if ! command -v python3 >/dev/null 2>&1; then
  echo "Error: Python 3.12+ required. Install from https://www.python.org/"
  exit 1
fi

PYTHON_VERSION=$(python3 --version | awk '{print $2}')
if [ "$(printf '%s\n' "3.12" "$PYTHON_VERSION" | sort -V | head -n1)" != "3.12" ]; then
  echo "Error: Python 3.12+ required. Current version: $PYTHON_VERSION"
  exit 1
fi

# Install pipx if not present
if ! command -v pipx >/dev/null 2>&1; then
  echo "pipx not found. Installing..."

  if [ "$OS" = "darwin" ]; then
    if ! command -v brew >/dev/null 2>&1; then
      echo "Error: Homebrew is not installed. Install it from https://brew.sh/"
      exit 1
    fi
    brew install pipx
  else
    python3 -m pip install --user pipx
  fi
fi

# Ensure pipx is usable in THIS shell (critical fix)
export PATH="$HOME/.local/bin:$PATH"

# Also ensure pipx paths are configured
python3 -m pipx ensurepath >/dev/null 2>&1 || true

# Verify pipx works
if ! command -v pipx >/dev/null 2>&1; then
  echo "Error: pipx is installed but not available in PATH."
  echo "Try restarting your terminal or running:"
  echo "export PATH=\"\$HOME/.local/bin:\$PATH\""
  exit 1
fi

# Install Bernstein
echo "Installing Bernstein..."
pipx install bernstein

echo ""
echo "Bernstein installed successfully! 🎉"
echo ""
echo "Try:"
echo "  bernstein --version"
echo "  bernstein -g 'your goal here'"