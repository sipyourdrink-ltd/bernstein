"""Tests for issue #2366: dashboard authentication and role-based access.

Covers the four acceptance criteria:

* AC1 -- a non-loopback bind refuses unauthenticated requests and refuses to
  start without auth configured (posture projection + ``gui serve`` gate).
* AC2 -- a read-only (viewer) token cannot trigger any state-changing action,
  enforced per route and per credential kind (bearer token and session).
* AC3 -- operator actions land in the governance decision journal and the
  audit chain with the acting principal attached, and the whole run
  recomputes via ``verify_governance``.
* AC4 -- covered by unskipping ``TestDashboardAuth`` in
  ``test_web_api_enhancements.py`` (same PR).

Plus the substrate properties the feature leans on: scoped token issuance is
an append-only journal of HMAC-signed records (tamper-evident, never storing
the raw token), and every projection is deterministic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

import pytest
from httpx import ASGITransport, AsyncClient

from bernstein.core.security.audit import load_or_create_audit_key
from bernstein.core.security.audit_chain import EVENT_GOVERNANCE_DECISION, AuditChainStore
from bernstein.core.security.governance import read_decisions, verify_governance
from bernstein.core.server.dashboard_tokens import (
    ACTION_LOGIN,
    ACTION_WRITE,
    DASHBOARD_AUTH_RUN_ID,
    SCOPE_OPERATOR,
    SCOPE_VIEWER,
    DashboardTokenRegistry,
    dashboard_role_bindings,
    is_loopback_host,
    resolve_dashboard_posture,
)

if TYPE_CHECKING:
    from pathlib import Path


class SddEnv(NamedTuple):
    """Per-test .sdd workspace paths."""

    sdd: Path
    jsonl: Path
    key: bytes


@pytest.fixture()
def sdd_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SddEnv:
    """Isolated .sdd workspace with a pinned audit key and no password env."""
    sdd = tmp_path / ".sdd"
    (sdd / "runtime").mkdir(parents=True)
    key_path = tmp_path / "audit.key"
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(key_path))
    monkeypatch.delenv("BERNSTEIN_DASHBOARD_PASSWORD", raising=False)
    key = load_or_create_audit_key(key_path)
    return SddEnv(sdd=sdd, jsonl=sdd / "runtime" / "tasks.jsonl", key=key)


def _registry(env: SddEnv) -> DashboardTokenRegistry:
    return DashboardTokenRegistry(env.sdd / "auth" / "dashboard_tokens.jsonl", hmac_key=env.key)


def _client_for(env: SddEnv) -> AsyncClient:
    from bernstein.core.server import create_app

    app = create_app(jsonl_path=env.jsonl)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ============================================================================
# Scoped token registry: signed, append-only, tamper-evident
# ============================================================================


class TestScopedTokenRegistry:
    """The token journal is the receipt: signed rows, no raw token at rest."""

    def test_issue_and_validate_roundtrip(self, sdd_env: SddEnv) -> None:
        registry = _registry(sdd_env)
        raw, record = registry.issue(principal="alice", scope=SCOPE_OPERATOR, now=1000)
        assert record.principal == "alice"
        assert record.scope == SCOPE_OPERATOR
        assert record.token_id
        resolved = registry.validate(raw)
        assert resolved is not None
        assert resolved.principal == "alice"
        assert resolved.scope == SCOPE_OPERATOR

    def test_raw_token_never_stored(self, sdd_env: SddEnv) -> None:
        registry = _registry(sdd_env)
        raw, _ = registry.issue(principal="alice", scope=SCOPE_VIEWER, now=1000)
        journal_text = (sdd_env.sdd / "auth" / "dashboard_tokens.jsonl").read_text(encoding="utf-8")
        assert raw not in journal_text

    def test_validate_rejects_unknown_token(self, sdd_env: SddEnv) -> None:
        registry = _registry(sdd_env)
        registry.issue(principal="alice", scope=SCOPE_VIEWER, now=1000)
        assert registry.validate("not-a-real-token") is None

    def test_validate_is_verbatim_no_stripping(self, sdd_env: SddEnv) -> None:
        registry = _registry(sdd_env)
        raw, _ = registry.issue(principal="alice", scope=SCOPE_VIEWER, now=1000)
        assert registry.validate(raw + "\n") is None
        assert registry.validate(" " + raw) is None

    def test_revoked_token_stops_validating(self, sdd_env: SddEnv) -> None:
        registry = _registry(sdd_env)
        raw, record = registry.issue(principal="alice", scope=SCOPE_OPERATOR, now=1000)
        assert registry.revoke(record.token_id, now=1001) is True
        assert registry.validate(raw) is None

    def test_revoke_unknown_id_returns_false(self, sdd_env: SddEnv) -> None:
        registry = _registry(sdd_env)
        assert registry.revoke("deadbeefdead", now=1001) is False

    def test_tampered_scope_fails_validation(self, sdd_env: SddEnv) -> None:
        """Widening viewer -> operator by editing the journal is detected."""
        registry = _registry(sdd_env)
        raw, _ = registry.issue(principal="mallory", scope=SCOPE_VIEWER, now=1000)
        journal = sdd_env.sdd / "auth" / "dashboard_tokens.jsonl"
        text = journal.read_text(encoding="utf-8")
        assert '"viewer"' in text
        journal.write_text(text.replace('"viewer"', '"operator"'), encoding="utf-8")
        assert registry.validate(raw) is None

    def test_has_tokens_reflects_journal(self, sdd_env: SddEnv) -> None:
        registry = _registry(sdd_env)
        assert registry.has_tokens() is False
        registry.issue(principal="alice", scope=SCOPE_VIEWER, now=1000)
        assert registry.has_tokens() is True

    def test_records_are_signed(self, sdd_env: SddEnv) -> None:
        registry = _registry(sdd_env)
        registry.issue(principal="alice", scope=SCOPE_VIEWER, now=1000)
        registry.issue(principal="bob", scope=SCOPE_OPERATOR, now=1001)
        records = registry.records()
        assert len(records) == 2
        assert all(r.verify_signature(sdd_env.key) for r in records)

    def test_rejects_unknown_scope(self, sdd_env: SddEnv) -> None:
        registry = _registry(sdd_env)
        with pytest.raises(ValueError, match="scope"):
            registry.issue(principal="alice", scope="root", now=1000)


# ============================================================================
# AC1: startup posture -- loopback vs non-loopback
# ============================================================================


class TestStartupPosture:
    """Non-loopback binds refuse to start without auth; loopback gets a token."""

    @pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "127.1.2.3"])
    def test_loopback_hosts_detected(self, host: str) -> None:
        assert is_loopback_host(host) is True

    @pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.4", "10.0.0.7", "fleet.example.com", "::"])
    def test_non_loopback_hosts_detected(self, host: str) -> None:
        assert is_loopback_host(host) is False

    @pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
    def test_loopback_unconfigured_generates_token(self, host: str) -> None:
        assert resolve_dashboard_posture(host, auth_configured=False) == "generate"

    @pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.4", "::"])
    def test_non_loopback_unconfigured_refuses(self, host: str) -> None:
        assert resolve_dashboard_posture(host, auth_configured=False) == "refuse"

    @pytest.mark.parametrize("host", ["127.0.0.1", "0.0.0.0"])
    def test_configured_binds_start(self, host: str) -> None:
        assert resolve_dashboard_posture(host, auth_configured=True) == "configured"

    def test_gui_serve_refuses_non_loopback_without_auth(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import uvicorn
        from click.testing import CliRunner

        from bernstein.gui.cli import serve

        called: list[object] = []
        monkeypatch.setattr(uvicorn, "run", lambda *a, **k: called.append(a))
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("BERNSTEIN_DASHBOARD_PASSWORD", raising=False)
        monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))

        result = CliRunner().invoke(serve, ["--host", "0.0.0.0", "--minimal", "--no-open"])
        assert result.exit_code != 0
        assert called == []
        assert "dashboard-token issue" in result.output

    def test_gui_serve_loopback_prints_generated_token(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import uvicorn
        from click.testing import CliRunner

        from bernstein.gui.cli import serve

        monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("BERNSTEIN_DASHBOARD_PASSWORD", raising=False)
        key_path = tmp_path / "audit.key"
        monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(key_path))

        result = CliRunner().invoke(serve, ["--host", "127.0.0.1", "--minimal", "--no-open"])
        assert result.exit_code == 0
        assert "Dashboard token" in result.output
        registry = DashboardTokenRegistry(
            tmp_path / ".sdd" / "auth" / "dashboard_tokens.jsonl",
            hmac_key=load_or_create_audit_key(key_path),
        )
        assert registry.has_tokens() is True

    def test_gui_serve_loopback_configured_does_not_reissue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import uvicorn
        from click.testing import CliRunner

        from bernstein.gui.cli import serve

        monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
        monkeypatch.chdir(tmp_path)
        key_path = tmp_path / "audit.key"
        monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(key_path))
        key = load_or_create_audit_key(key_path)
        registry = DashboardTokenRegistry(tmp_path / ".sdd" / "auth" / "dashboard_tokens.jsonl", hmac_key=key)
        registry.issue(principal="alice", scope=SCOPE_OPERATOR, now=1000)

        result = CliRunner().invoke(serve, ["--host", "127.0.0.1", "--minimal", "--no-open"])
        assert result.exit_code == 0
        assert len(registry.records()) == 1


# ============================================================================
# AC2: scope enforcement per route
# ============================================================================


class TestScopeEnforcement:
    """A viewer token reads but never writes; an operator token does both."""

    @pytest.fixture()
    def tokens(self, sdd_env: SddEnv) -> dict[str, str]:
        registry = _registry(sdd_env)
        viewer_raw, _ = registry.issue(principal="viewer-vera", scope=SCOPE_VIEWER, now=1000)
        operator_raw, _ = registry.issue(principal="operator-olga", scope=SCOPE_OPERATOR, now=1001)
        return {"viewer": viewer_raw, "operator": operator_raw}

    @pytest.mark.anyio
    @pytest.mark.parametrize("route", ["/dashboard/data", "/dashboard/file_locks"])
    async def test_viewer_token_reads_route(self, sdd_env: SddEnv, tokens: dict[str, str], route: str) -> None:
        async with _client_for(sdd_env) as client:
            resp = await client.get(route, headers={"Authorization": f"Bearer {tokens['viewer']}"})
            assert resp.status_code == 200

    @pytest.mark.anyio
    @pytest.mark.parametrize("route", ["/dashboard/data", "/dashboard/file_locks", "/dashboard/actions/retry"])
    async def test_viewer_token_cannot_write_route(self, sdd_env: SddEnv, tokens: dict[str, str], route: str) -> None:
        async with _client_for(sdd_env) as client:
            resp = await client.post(route, headers={"Authorization": f"Bearer {tokens['viewer']}"})
            assert resp.status_code == 403

    @pytest.mark.anyio
    @pytest.mark.parametrize("route", ["/dashboard/data", "/dashboard/file_locks"])
    async def test_operator_token_write_passes_authz(self, sdd_env: SddEnv, tokens: dict[str, str], route: str) -> None:
        """Operator scope clears the authz gate (route itself may 405)."""
        async with _client_for(sdd_env) as client:
            resp = await client.post(route, headers={"Authorization": f"Bearer {tokens['operator']}"})
            assert resp.status_code not in (401, 403)

    @pytest.mark.anyio
    async def test_unauthenticated_request_rejected_when_tokens_exist(
        self, sdd_env: SddEnv, tokens: dict[str, str]
    ) -> None:
        async with _client_for(sdd_env) as client:
            resp = await client.get("/dashboard/data")
            assert resp.status_code == 401

    @pytest.mark.anyio
    async def test_viewer_session_cannot_write(self, sdd_env: SddEnv, tokens: dict[str, str]) -> None:
        """The session cookie wraps the token's scope: no privilege widening."""
        async with _client_for(sdd_env) as client:
            login = await client.post("/dashboard/auth/login", json={"token": tokens["viewer"]})
            assert login.status_code == 200
            resp = await client.post("/dashboard/data")
            assert resp.status_code == 403

    @pytest.mark.anyio
    async def test_token_login_reports_scope_and_principal(self, sdd_env: SddEnv, tokens: dict[str, str]) -> None:
        async with _client_for(sdd_env) as client:
            login = await client.post("/dashboard/auth/login", json={"token": tokens["viewer"]})
            assert login.status_code == 200
            data = login.json()
            assert data["authenticated"] is True
            assert data["scope"] == SCOPE_VIEWER
            assert data["principal"] == "viewer-vera"

    @pytest.mark.anyio
    async def test_invalid_token_login_rejected(self, sdd_env: SddEnv, tokens: dict[str, str]) -> None:
        async with _client_for(sdd_env) as client:
            login = await client.post("/dashboard/auth/login", json={"token": "bogus"})
            assert login.status_code == 401

    @pytest.mark.anyio
    async def test_session_cookie_is_hardened(self, sdd_env: SddEnv, tokens: dict[str, str]) -> None:
        """The session cookie is HttpOnly and SameSite, never sent in the clear.

        Over plain HTTP (the loopback dev bind) the cookie omits ``Secure`` so
        the session still round-trips; behind a TLS-terminating proxy the
        forwarded-proto header pins ``Secure`` so the session token never
        travels in clear text.
        """
        async with _client_for(sdd_env) as client:
            plain = await client.post("/dashboard/auth/login", json={"token": tokens["viewer"]})
            assert plain.status_code == 200
            cookie = plain.headers["set-cookie"]
            assert "bernstein_dashboard_session=" in cookie
            assert "httponly" in cookie.lower()
            assert "samesite=lax" in cookie.lower()
            assert "secure" not in cookie.lower()

            secure_login = await client.post(
                "/dashboard/auth/login",
                json={"token": tokens["viewer"]},
                headers={"X-Forwarded-Proto": "https"},
            )
            assert secure_login.status_code == 200
            secure_cookie = secure_login.headers["set-cookie"]
            assert "secure" in secure_cookie.lower()
            assert "httponly" in secure_cookie.lower()

    @pytest.mark.anyio
    async def test_versioned_dashboard_surface_enforced_too(self, sdd_env: SddEnv, tokens: dict[str, str]) -> None:
        async with _client_for(sdd_env) as client:
            ok = await client.get("/api/v1/dashboard/data", headers={"Authorization": f"Bearer {tokens['viewer']}"})
            assert ok.status_code == 200
            denied = await client.post(
                "/api/v1/dashboard/data", headers={"Authorization": f"Bearer {tokens['viewer']}"}
            )
            assert denied.status_code == 403
            anon = await client.get("/api/v1/dashboard/data")
            assert anon.status_code == 401


# ============================================================================
# AC3: principal lands in the governance journal + audit chain
# ============================================================================


class TestPrincipalReceipts:
    """Every authz decision is an anchored governance record, not a log line."""

    @pytest.fixture()
    def tokens(self, sdd_env: SddEnv) -> dict[str, str]:
        registry = _registry(sdd_env)
        viewer_raw, _ = registry.issue(principal="viewer-vera", scope=SCOPE_VIEWER, now=1000)
        operator_raw, _ = registry.issue(principal="operator-olga", scope=SCOPE_OPERATOR, now=1001)
        return {"viewer": viewer_raw, "operator": operator_raw}

    @pytest.mark.anyio
    async def test_operator_write_is_journaled_with_principal(self, sdd_env: SddEnv, tokens: dict[str, str]) -> None:
        async with _client_for(sdd_env) as client:
            await client.post("/dashboard/data", headers={"Authorization": f"Bearer {tokens['operator']}"})
        rows = read_decisions(sdd_env.sdd / "lineage", DASHBOARD_AUTH_RUN_ID)
        writes = [r for r in rows if r.action == ACTION_WRITE and r.subject == "operator-olga"]
        assert writes, "operator write must be journaled with the acting principal"
        assert writes[0].verdict == "allow"
        assert writes[0].journal_entry_hash

    @pytest.mark.anyio
    async def test_denied_viewer_write_is_journaled(self, sdd_env: SddEnv, tokens: dict[str, str]) -> None:
        async with _client_for(sdd_env) as client:
            await client.post("/dashboard/data", headers={"Authorization": f"Bearer {tokens['viewer']}"})
        rows = read_decisions(sdd_env.sdd / "lineage", DASHBOARD_AUTH_RUN_ID)
        denies = [r for r in rows if r.action == ACTION_WRITE and r.subject == "viewer-vera"]
        assert denies
        assert denies[0].verdict == "deny"

    @pytest.mark.anyio
    async def test_failed_login_is_journaled_as_deny(self, sdd_env: SddEnv, tokens: dict[str, str]) -> None:
        async with _client_for(sdd_env) as client:
            await client.post("/dashboard/auth/login", json={"token": "bogus"})
        rows = read_decisions(sdd_env.sdd / "lineage", DASHBOARD_AUTH_RUN_ID)
        logins = [r for r in rows if r.action == ACTION_LOGIN]
        assert logins
        assert logins[0].verdict == "deny"

    @pytest.mark.anyio
    async def test_governance_run_recomputes(self, sdd_env: SddEnv, tokens: dict[str, str]) -> None:
        """The dashboard-auth run verifies end to end from the spine."""
        async with _client_for(sdd_env) as client:
            await client.post("/dashboard/data", headers={"Authorization": f"Bearer {tokens['operator']}"})
            await client.post("/dashboard/data", headers={"Authorization": f"Bearer {tokens['viewer']}"})
        result = verify_governance(
            run_id=DASHBOARD_AUTH_RUN_ID,
            lineage_root=sdd_env.sdd / "lineage",
            hmac_key=sdd_env.key,
            bindings=dashboard_role_bindings(sdd_env.key),
        )
        assert result.ok, result.reason

    @pytest.mark.anyio
    async def test_decision_mirrored_into_audit_chain(self, sdd_env: SddEnv, tokens: dict[str, str]) -> None:
        async with _client_for(sdd_env) as client:
            await client.post("/dashboard/data", headers={"Authorization": f"Bearer {tokens['operator']}"})
        chain = AuditChainStore(sdd_env.sdd / "audit", key=sdd_env.key)
        events = chain.query(event_type=EVENT_GOVERNANCE_DECISION)
        subjects = {e.details.get("subject") for e in events}
        assert "operator-olga" in subjects


# ============================================================================
# Determinism of the role projection
# ============================================================================


class TestDeterministicProjection:
    """Same key, same bindings: the policy identity is stable bytes."""

    def test_bindings_hash_is_stable(self, sdd_env: SddEnv) -> None:
        first = dashboard_role_bindings(sdd_env.key)
        second = dashboard_role_bindings(sdd_env.key)
        assert first.bindings_hash() == second.bindings_hash()
        assert first.verify_signature(sdd_env.key)

    def test_viewer_role_has_no_write_permission(self, sdd_env: SddEnv) -> None:
        bindings = dashboard_role_bindings(sdd_env.key)
        assert ACTION_WRITE not in bindings.role_permissions[SCOPE_VIEWER]
        assert ACTION_WRITE in bindings.role_permissions[SCOPE_OPERATOR]

    def test_identical_inputs_project_identical_decisions(self, tmp_path: Path) -> None:
        from bernstein.core.server.dashboard_tokens import DashboardGovernance

        key = b"0" * 32
        dicts: list[dict[str, object]] = []
        for sub in ("a", "b"):
            root = tmp_path / sub / "lineage"
            gov = DashboardGovernance(lineage_root=root, hmac_key=key)
            decision = gov.decide(subject="alice", scope=SCOPE_OPERATOR, action=ACTION_WRITE, now=1234)
            dicts.append(decision.to_dict())
        assert dicts[0] == dicts[1]


# ============================================================================
# CLI: bernstein auth dashboard-token
# ============================================================================


class TestDashboardTokenCli:
    """Issue / list / revoke scoped dashboard tokens from the CLI."""

    def test_issue_prints_token_once_and_journals(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from click.testing import CliRunner

        from bernstein.cli.commands.dashboard_token_cmd import dashboard_token_group

        key_path = tmp_path / "audit.key"
        monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(key_path))
        result = CliRunner().invoke(
            dashboard_token_group,
            ["issue", "--principal", "alice", "--scope", "operator", "--workdir", str(tmp_path)],
        )
        assert result.exit_code == 0, result.output
        registry = DashboardTokenRegistry(
            tmp_path / ".sdd" / "auth" / "dashboard_tokens.jsonl",
            hmac_key=load_or_create_audit_key(key_path),
        )
        records = registry.records()
        assert len(records) == 1
        assert records[0].principal == "alice"
        # The raw token is printed exactly once and never journaled.
        journal_text = (tmp_path / ".sdd" / "auth" / "dashboard_tokens.jsonl").read_text(encoding="utf-8")
        token_lines = [ln for ln in result.output.splitlines() if ln.strip().startswith("Token:")]
        assert len(token_lines) == 1
        raw = token_lines[0].split("Token:", 1)[1].strip()
        assert raw
        assert raw not in journal_text
        assert registry.validate(raw) is not None

    def test_list_shows_metadata_not_tokens(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from click.testing import CliRunner

        from bernstein.cli.commands.dashboard_token_cmd import dashboard_token_group

        key_path = tmp_path / "audit.key"
        monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(key_path))
        key = load_or_create_audit_key(key_path)
        registry = DashboardTokenRegistry(tmp_path / ".sdd" / "auth" / "dashboard_tokens.jsonl", hmac_key=key)
        raw, record = registry.issue(principal="bob", scope=SCOPE_VIEWER, now=1000)

        result = CliRunner().invoke(dashboard_token_group, ["list", "--workdir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "bob" in result.output
        assert record.token_id in result.output
        assert raw not in result.output

    def test_revoke_blocks_future_validation(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from click.testing import CliRunner

        from bernstein.cli.commands.dashboard_token_cmd import dashboard_token_group

        key_path = tmp_path / "audit.key"
        monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(key_path))
        key = load_or_create_audit_key(key_path)
        registry = DashboardTokenRegistry(tmp_path / ".sdd" / "auth" / "dashboard_tokens.jsonl", hmac_key=key)
        raw, record = registry.issue(principal="bob", scope=SCOPE_VIEWER, now=1000)

        result = CliRunner().invoke(dashboard_token_group, ["revoke", record.token_id, "--workdir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert registry.validate(raw) is None
