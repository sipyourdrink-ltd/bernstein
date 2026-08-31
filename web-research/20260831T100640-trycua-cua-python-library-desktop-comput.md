# Web research

**Query:** trycua cua python library desktop computer use agent sandbox

**Sources:**
- https://github.com/trycua/cua
- https://pypi.org/project/cua/

**Direct Answer**

The `cua` package on PyPI is the unified Python SDK for Computer-Use Agents from trycua. It provides:
- **Sandbox management** (`cua-sandbox`): ephemeral local VM/container sandboxes (`Sandbox.ephemeral(Image.linux(), local=True)`)
- **Computer-use agent** (`cua-agent`): LLM-driven agent (`ComputerAgent`) that can drive a desktop via the sandbox
- **CLI** (`cua-cli`): `cua` command for managing sandboxes and images

Installation: `pip install cua` (requires Python 3.12+; for 3.11 install `cua-sandbox` directly). Extras: `[omni]` for SOM visual grounding, `[uitars-mlx]`/`[uitars-hf]` for UiTars models, `[all]` for everything.

The GitHub repo (trycua/cua) is the monorepo containing the SDK, drivers, sandbox infrastructure, and cloud fleet components. It shows active development (4,500+ commits, recent commits Aug 2026) and includes sandbox image API contracts and cross-platform drivers for macOS/Windows/Linux.

**Current Best Practice (from sources)**
- Use the meta-package `cua` for a batteries-included experience; pin extras you need (`cua[omni]`, `cua[uitars-mlx]`, etc.).
- Run ephemeral local sandboxes for isolation: `async with Sandbox.ephemeral(Image.linux(), local=True) as sb:`.
- Opt out of telemetry if desired: `export CUA_TELEMETRY_ENABLED=false` or `telemetry_enabled=False` per instance.
- Documentation at https://docs.trycua.com/.

**Contradictions / Gaps**
- The GitHub README content failed to load (error page), so high-level architecture, driver details, and quickstart steps aren't verifiable from Source 1.
- PyPI shows version 0.1.6 (Apr 2026) as latest; GitHub shows recent commits (Aug 2026) — the PyPI release may lag behind main.
- No explicit mention of "desktop" vs. "headless" sandbox modes in the PyPI snippet; the GitHub repo references cross-platform drivers but details are unavailable.

**Unverified**
- Exact sandbox backend (VM vs. container), GPU/acceleration support, and persistence model.
- Authentication/secret handling for cloud fleet vs. local-only use.
- Maturity/stability of `ComputerAgent` with different LLM backends (OpenAI, Anthropic, Gemini, UiTars).
- Licensing of bundled drivers (MIT for the SDK per PyPI; driver binaries may differ).
