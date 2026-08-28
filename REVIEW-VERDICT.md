FIXED: 1 of 1 blocking findings

Blocking findings
- Missing release-notes fragment: created `docs/release-notes/fragments/4700-volunteer-hub-http.md` declaring the `bernstein volunteer hub` subcommand and its FastAPI surface.

Nits addressed (non-blocking)
- `hub_app.py:181` docstring: `future扩展 (TLS settings, auth config, etc.)` → `future config (TLS, auth, etc.)`.
- Removed dead `EnrollRequest` / `SubmitRequest` dataclasses; none were referenced anywhere in the module.
- `hub_app.py:232` `approve_worker`: no longer claims `SCOPE_VOLUNTEER_ENROLL` or "operator-only" framing; docstring now accurately describes a placeholder that verifies worker enrollment only, with no approval state in the store.
- `hub_app.py:174` `build_hub_app`: added optional `authenticator` parameter that wires `VolunteerAuthenticator` into `app.state.volunteer_authenticator`; docstring states auth is deferred until a token-issuance surface exists.
- `volunteer_cmd.py:278,296` `hub_cmd`: added `.. note::` docstring and runtime `click.echo` warning that the lease store is single-process only (no `--workers N>1` / replicas).

Verification
- `uv run pytest tests/unit/volunteer/test_hub_app.py` → 18 passed.
- `uv run pytest tests/unit/volunteer/test_volunteer_cli.py` → 27 passed.
- `uv run ruff check src/bernstein/core/volunteer/hub_app.py src/bernstein/cli/commands/volunteer_cmd.py` → All checks passed.