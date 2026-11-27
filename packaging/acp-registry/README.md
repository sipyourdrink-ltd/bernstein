# ACP Registry package assets

`bernstein/agent.json` and `bernstein/icon.svg` in this directory are prepared
for submission to the [Agent Client Protocol Registry](https://github.com/agentclientprotocol/registry).

They are kept in-tree under `packaging/acp-registry/` and are **not** submitted
until a Bernstein release containing the full ACP v1 bridge (wire transport,
standalone runner, and session updates) is published to PyPI.

## Submission checklist (maintainers)

1. Confirm the target Bernstein release is published on PyPI and passes the
   local ACP v1 conformance suite:

   ```bash
   uv run pytest tests/integration/acp/test_acp_v1_conformance.py tests/integration/acp/test_acp_v1_sdk.py
   ```

2. Confirm `version` and `distribution.uvx.package` in
   `packaging/acp-registry/bernstein/agent.json` match the published PyPI
   release version (`scripts/bump_version.py` and
   `scripts/gen_distribution_manifests.py` keep them synchronized with
   `pyproject.toml`).

3. Open a pull request against `agentclientprotocol/registry` copying
   `bernstein/agent.json` and `bernstein/icon.svg` to the top-level
   `bernstein/` directory of the registry repository. Do not add an `icon`
   field to `agent.json`; the registry build script populates it from
   `icon.svg` automatically.

## Validation rules mirrored in CI

The `ACP v1 conformance` job in `.github/workflows/ci.yml` checks:

- `agent.json` required fields, semver alignment with `pyproject.toml`, and
  `distribution.uvx` entry (`bernstein acp serve --stdio`).
- `icon.svg` dimensions (`16x16`, `viewBox="0 0 16 16"`), monochrome
  `currentColor` styling, and absence of XML comments.
- Live `run_auth_check` initialize handshake and strict JSON-RPC 2.0 `stdout`
  purity against `bernstein acp serve --stdio`.
