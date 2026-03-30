#!/usr/bin/env bash
# Build .deb and .rpm packages for Bernstein.
#
# Usage:
#   ./scripts/build_packages.sh [VERSION]
#
# Requirements (install once):
#   sudo apt-get install ruby ruby-dev rpm gnupg2 dpkg-dev createrepo-c
#   sudo gem install fpm
#
# GPG signing (optional):
#   export GPG_KEY_ID="<your-key-fingerprint>"
#   export GPG_PASSPHRASE="<passphrase>"
#
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# ── Version ─────────────────────────────────────────────────────────────────
VERSION="${1:-}"
if [ -z "$VERSION" ]; then
  VERSION=$(grep '^version = ' pyproject.toml | head -1 | sed 's/version = "\(.*\)"/\1/')
fi
echo "==> Building packages for bernstein v${VERSION}"

# ── Prerequisite check ───────────────────────────────────────────────────────
for cmd in fpm python3 dpkg-scanpackages createrepo_c gpg2; do
  if ! command -v "$cmd" &>/dev/null; then
    echo "ERROR: '$cmd' not found." >&2
    echo "  Install: sudo apt-get install ruby-dev rpm dpkg-dev createrepo-c && sudo gem install fpm" >&2
    exit 1
  fi
done

# ── Build Python wheel ───────────────────────────────────────────────────────
echo "==> Building Python wheel"
uv build --wheel
WHEEL=$(ls dist/bernstein-*.whl | head -1)
echo "    Wheel: $WHEEL"

# ── Staging directory ────────────────────────────────────────────────────────
STAGING="$ROOT_DIR/build/staging"
rm -rf "$STAGING"

echo "==> Creating bundled virtualenv in staging"
mkdir -p "$STAGING/opt/bernstein"
python3 -m venv "$STAGING/opt/bernstein/venv"
"$STAGING/opt/bernstein/venv/bin/pip" install --quiet "$WHEEL"

mkdir -p "$STAGING/usr/bin"
cat > "$STAGING/usr/bin/bernstein" << 'WRAPPER'
#!/bin/bash
exec /opt/bernstein/venv/bin/bernstein "$@"
WRAPPER
chmod +x "$STAGING/usr/bin/bernstein"

# ── Build .deb ───────────────────────────────────────────────────────────────
echo "==> Building .deb"
mkdir -p build/packages
fpm -s dir -t deb \
  -n bernstein \
  -v "$VERSION" \
  --iteration 1 \
  --description "Declarative agent orchestration for engineering teams" \
  --url "https://github.com/chernistry/bernstein" \
  --maintainer "Alex Chernysh <alex@alexchernysh.com>" \
  --license Apache-2.0 \
  --category utils \
  --architecture amd64 \
  --package build/packages/ \
  --prefix / \
  -C "$STAGING" \
  opt/bernstein \
  usr/bin/bernstein

DEB=$(ls build/packages/bernstein_*.deb | head -1)
echo "    Built: $DEB"

# ── Build .rpm ───────────────────────────────────────────────────────────────
echo "==> Building .rpm"
fpm -s dir -t rpm \
  -n bernstein \
  -v "$VERSION" \
  --iteration 1 \
  --description "Declarative agent orchestration for engineering teams" \
  --url "https://github.com/chernistry/bernstein" \
  --maintainer "Alex Chernysh <alex@alexchernysh.com>" \
  --license Apache-2.0 \
  --category utils \
  --architecture x86_64 \
  --package build/packages/ \
  --prefix / \
  -C "$STAGING" \
  opt/bernstein \
  usr/bin/bernstein

RPM=$(ls build/packages/bernstein-*.rpm | head -1)
echo "    Built: $RPM"

# ── GPG signing ──────────────────────────────────────────────────────────────
if [ -n "${GPG_KEY_ID:-}" ] && [ -n "${GPG_PASSPHRASE:-}" ]; then
  echo "==> Signing packages (GPG key: $GPG_KEY_ID)"

  echo "$GPG_PASSPHRASE" > /tmp/_gpg_pass_$$
  chmod 600 /tmp/_gpg_pass_$$

  cat > ~/.rpmmacros << EOF
%_gpg_name $GPG_KEY_ID
%_gpg_path $HOME/.gnupg
%__gpg /usr/bin/gpg2
%__gpg_sign_cmd %{__gpg} --batch --no-verbose --passphrase-file /tmp/_gpg_pass_$$ \
  --pinentry-mode loopback -u "%{_gpg_name}" \
  -sbo %{__signature_filename} %{__plaintext_filename}
EOF

  rpmsign --addsign "$RPM"
  rm -f /tmp/_gpg_pass_$$

  gpg --armor --export "$GPG_KEY_ID" > build/packages/bernstein-signing-key.gpg
  echo "    Public key: build/packages/bernstein-signing-key.gpg"
else
  echo "    GPG_KEY_ID / GPG_PASSPHRASE not set — skipping signing"
fi

# ── APT repository metadata ──────────────────────────────────────────────────
echo "==> Generating APT repository metadata"
APTREPO="build/repo/apt"
mkdir -p "$APTREPO/pool/main" "$APTREPO/dists/stable/main/binary-amd64"
cp "$DEB" "$APTREPO/pool/main/"

dpkg-scanpackages --arch amd64 "$APTREPO/pool/main" \
  > "$APTREPO/dists/stable/main/binary-amd64/Packages"
gzip  -9kc "$APTREPO/dists/stable/main/binary-amd64/Packages" \
  > "$APTREPO/dists/stable/main/binary-amd64/Packages.gz"
bzip2 -kc  "$APTREPO/dists/stable/main/binary-amd64/Packages" \
  > "$APTREPO/dists/stable/main/binary-amd64/Packages.bz2"

{
  echo "Origin: Bernstein"
  echo "Label: Bernstein"
  echo "Suite: stable"
  echo "Codename: stable"
  echo "Version: $VERSION"
  echo "Architectures: amd64"
  echo "Components: main"
  echo "Description: Bernstein APT repository"
  echo "Date: $(date -u -R)"
  echo "MD5Sum:"
  for f in \
    "$APTREPO/dists/stable/main/binary-amd64/Packages" \
    "$APTREPO/dists/stable/main/binary-amd64/Packages.gz" \
    "$APTREPO/dists/stable/main/binary-amd64/Packages.bz2"; do
    printf " %s %d %s\n" "$(md5sum "$f" | cut -d' ' -f1)" "$(wc -c < "$f")" "${f#$APTREPO/dists/stable/}"
  done
  echo "SHA256:"
  for f in \
    "$APTREPO/dists/stable/main/binary-amd64/Packages" \
    "$APTREPO/dists/stable/main/binary-amd64/Packages.gz" \
    "$APTREPO/dists/stable/main/binary-amd64/Packages.bz2"; do
    printf " %s %d %s\n" "$(sha256sum "$f" | cut -d' ' -f1)" "$(wc -c < "$f")" "${f#$APTREPO/dists/stable/}"
  done
} > "$APTREPO/dists/stable/Release"

if [ -n "${GPG_KEY_ID:-}" ] && [ -n "${GPG_PASSPHRASE:-}" ]; then
  echo "$GPG_PASSPHRASE" | gpg --batch --passphrase-fd 0 --pinentry-mode loopback \
    --armor --detach-sign -o "$APTREPO/dists/stable/Release.gpg" "$APTREPO/dists/stable/Release"
  echo "$GPG_PASSPHRASE" | gpg --batch --passphrase-fd 0 --pinentry-mode loopback \
    --clearsign -o "$APTREPO/dists/stable/InRelease" "$APTREPO/dists/stable/Release"
  echo "    APT Release signed"
fi

# ── YUM/DNF repository metadata ──────────────────────────────────────────────
echo "==> Generating YUM/DNF repository metadata"
RPMREPO="build/repo/rpm"
mkdir -p "$RPMREPO/packages"
cp "$RPM" "$RPMREPO/packages/"
createrepo_c "$RPMREPO"

if [ -n "${GPG_KEY_ID:-}" ] && [ -n "${GPG_PASSPHRASE:-}" ]; then
  echo "$GPG_PASSPHRASE" | gpg --batch --passphrase-fd 0 --pinentry-mode loopback \
    --armor --detach-sign "$RPMREPO/repodata/repomd.xml"
  echo "    repomd.xml signed"
fi

# ── Summary ──────────────────────────────────────────────────────────────────
echo ""
echo "==> Done"
echo "    .deb:        $DEB"
echo "    .rpm:        $RPM"
echo "    APT repo:    build/repo/apt/"
echo "    YUM repo:    build/repo/rpm/"
echo ""
echo "Local test:"
echo "    sudo dpkg -i $DEB         # Ubuntu/Debian"
echo "    sudo rpm -i $RPM          # RHEL/Fedora"
