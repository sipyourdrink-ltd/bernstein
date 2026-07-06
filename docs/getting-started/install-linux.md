# Installing Bernstein on Linux

Bernstein installs on Linux through pip/uv/pipx, Homebrew, Fedora COPR, or Docker.
All of these pull from published artifacts (PyPI, the Homebrew tap, the COPR build, and the
container registry), so there is nothing to configure by hand.

---

## Recommended: pip / uv / pipx

Any of these installs Bernstein from PyPI. `uv` and `pipx` keep it in an isolated environment.

```bash
uv tool install bernstein     # isolated tool environment (recommended)
pipx install bernstein        # isolated, if you already use pipx
pip install bernstein         # into the current environment
```

### Verify the installation

```bash
bernstein --version
```

---

## Debian / Ubuntu

There is no APT repository. Use `uv tool install bernstein` or `pipx install bernstein`
(both shown above), or run the Docker image described below.

---

## Fedora / RHEL via COPR

```bash
sudo dnf copr enable alexchernysh/bernstein
sudo dnf install bernstein
```

COPR repository: https://copr.fedorainfracloud.org/coprs/alexchernysh/bernstein/

---

## Homebrew (Linux or macOS)

```bash
brew install chernistry/homebrew-tap/bernstein
```

Bernstein is not in `homebrew-core`; the tap is published from PyPI on each release.

---

## Docker

```bash
docker run -v "$(pwd)":/workspace -p 8052:8052 \
  ghcr.io/sipyourdrink-ltd/bernstein -g "your goal"
```

---

## Other installation methods

| Method | Command |
|--------|---------|
| pipx   | `pipx install bernstein` |
| uv     | `uv tool install bernstein` |
| pip    | `pip install bernstein` |
| npm    | `npx bernstein-orchestrator` (requires Python 3.12+) |
| Docker | `docker run -v "$(pwd)":/workspace -p 8052:8052 ghcr.io/sipyourdrink-ltd/bernstein -g "your goal"` |
