# Installing Bernstein on Linux via APT / YUM

Bernstein publishes signed `.deb` and `.rpm` packages on every release, hosted on GitHub Pages.

> **Heads-up**: packages bundle a self-contained Python virtualenv under `/opt/bernstein/`, so there are no pip or pyproject.toml dependencies to manage.

---

## Required: one-time setup

Before adding the repository, configure GPG key verification so apt/dnf can authenticate packages.

### 1. Add the signing key

```bash
# Download and install the Bernstein GPG public key
curl -fsSL https://chernistry.github.io/bernstein/gpg/bernstein-signing-key.gpg \
  | sudo gpg --dearmor -o /usr/share/keyrings/bernstein-archive-keyring.gpg
```

---

## Debian / Ubuntu (APT)

```bash
# Add repository
echo "deb [arch=amd64 signed-by=/usr/share/keyrings/bernstein-archive-keyring.gpg] \
  https://chernistry.github.io/bernstein/apt stable main" \
  | sudo tee /etc/apt/sources.list.d/bernstein.list

# Install
sudo apt-get update
sudo apt-get install bernstein
```

### Verify the installation

```bash
bernstein --version
```

---

## Fedora / RHEL via COPR (recommended)

The easiest way to install on Fedora or RHEL. Targets: Fedora 41, 42 (x86_64, aarch64), EPEL 9, 10.

```bash
sudo dnf copr enable alexchernysh/bernstein
sudo dnf install bernstein
```

COPR repository: https://copr.fedorainfracloud.org/coprs/alexchernysh/bernstein/

## RHEL / Fedora / CentOS — manual RPM repo (alternative)

```bash
# Add repository
sudo tee /etc/yum.repos.d/bernstein.repo << 'EOF'
[bernstein]
name=Bernstein
baseurl=https://chernistry.github.io/bernstein/rpm
enabled=1
gpgcheck=1
gpgkey=https://chernistry.github.io/bernstein/gpg/bernstein-signing-key.gpg
EOF

# Install
sudo dnf install bernstein        # Fedora / RHEL 8+
# or
sudo yum install bernstein        # CentOS 7 / older RHEL
```

### Verify the installation

```bash
bernstein --version
```

---

## Direct download (no repository)

Download the latest package directly from [GitHub Releases](https://github.com/chernistry/bernstein/releases/latest):

```bash
# Debian/Ubuntu
curl -LO https://github.com/chernistry/bernstein/releases/latest/download/bernstein_amd64.deb
sudo dpkg -i bernstein_amd64.deb

# RHEL/Fedora
curl -LO https://github.com/chernistry/bernstein/releases/latest/download/bernstein-x86_64.rpm
sudo rpm -i bernstein-x86_64.rpm
```

---

## Verifying package signatures

```bash
# Verify the GPG signature on a .deb
gpg --verify bernstein_*.deb.asc

# Verify RPM signature
rpm -K bernstein-*.rpm
```

---

## Other installation methods

| Method | Command |
|--------|---------|
| pipx   | `pipx install bernstein` |
| uv     | `uv tool install bernstein` |
| pip    | `pip install bernstein` |
| npm    | `npx bernstein-orchestrator` (requires Python 3.12+) |
| Docker | `docker run ghcr.io/chernistry/bernstein` |

---

## Repository setup (for maintainers)

The `packages` branch of this repository is served via GitHub Pages and acts as both the APT and YUM repository host. The CI workflow `.github/workflows/publish-packages.yml` builds and publishes packages automatically on every `v*` tag.

Required repository secrets:

| Secret | Description |
|--------|-------------|
| `GPG_PRIVATE_KEY` | ASCII-armored GPG private key (`gpg --armor --export-secret-keys KEY_ID`) |
| `GPG_PASSPHRASE`  | Passphrase protecting the private key |
| `GPG_KEY_ID`      | Key fingerprint (used in rpm macros) |

GitHub Pages must be enabled on the `packages` branch in **Settings → Pages → Source**.

To build packages locally:

```bash
export GPG_KEY_ID="<your-key-fingerprint>"
export GPG_PASSPHRASE="<passphrase>"
./scripts/build_packages.sh
```
