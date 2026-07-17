"""Spawn-time drift-refusal gate + airgap-unchanged tests (issue #2518).

The spawn gate (``AgentSpawner._preflight_posture_drift``) only reads
``self._workdir`` and the process environment, so it is exercised here through
a lightweight shim carrying just that attribute -- no full orchestrator boot.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from bernstein.core.agents.spawner_core import AgentSpawner
from bernstein.core.security.audit_chain import AuditChainStore
from bernstein.core.security.deployment_profile import (
    SOVEREIGN_PROFILE,
    PostureDriftRefusal,
    build_posture_attestation,
    load_config_snapshot,
    resolve_effective_policy,
)
from bernstein.core.security.network_policy import (
    ENV_SOVEREIGN_MODE,
    is_airgap_profile,
    is_sovereign_profile,
)


def _preflight(workdir: Path) -> None:
    """Invoke the gate against a shim exposing only ``_workdir``."""
    AgentSpawner._preflight_posture_drift(SimpleNamespace(_workdir=workdir))  # type: ignore[arg-type]


def _attest_clean(tmp_path: Path) -> None:
    (tmp_path / ".sdd" / "audit").mkdir(parents=True, exist_ok=True)
    (tmp_path / "bernstein.yaml").write_text("goal: x\nstorage:\n  backend: memory\n", encoding="utf-8")
    policy = resolve_effective_policy(SOVEREIGN_PROFILE, load_config_snapshot(tmp_path))
    build_posture_attestation(
        workdir=tmp_path, policy=policy, timestamp=1, chain=AuditChainStore(tmp_path / ".sdd" / "audit")
    )


def test_gate_is_noop_when_not_sovereign(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_SOVEREIGN_MODE, raising=False)
    # No attestation, drifted config -- but sovereign is off, so the gate passes.
    (tmp_path / ".sdd" / "audit").mkdir(parents=True, exist_ok=True)
    _preflight(tmp_path)  # must not raise


def test_gate_passes_when_posture_matches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_SOVEREIGN_MODE, "1")
    _attest_clean(tmp_path)
    _preflight(tmp_path)  # posture matches attestation -> no refusal


def test_gate_refuses_on_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC3: a config edit after attestation blocks the next spawn with a receipt."""
    monkeypatch.setenv(ENV_SOVEREIGN_MODE, "1")
    _attest_clean(tmp_path)
    # Add a cloud storage sink after attestation.
    (tmp_path / "bernstein.yaml").write_text(
        "goal: x\nstorage:\n  backend: postgres\n  database_url: postgres://db.cloud/x\n", encoding="utf-8"
    )
    with pytest.raises(PostureDriftRefusal) as excinfo:
        _preflight(tmp_path)
    assert "storage_backend" in "".join(excinfo.value.record["diverging_keys"])
    assert excinfo.value.record_sha256.startswith("sha256:")


def test_gate_refuses_when_never_attested(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_SOVEREIGN_MODE, "1")
    (tmp_path / ".sdd" / "audit").mkdir(parents=True, exist_ok=True)
    (tmp_path / "bernstein.yaml").write_text("goal: x\n", encoding="utf-8")
    with pytest.raises(PostureDriftRefusal):
        _preflight(tmp_path)


def test_gate_refuses_on_revoked_certification_without_config_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC4: a gated endpoint with no receipt is refused even when the hash matches."""
    monkeypatch.setenv(ENV_SOVEREIGN_MODE, "1")
    (tmp_path / ".sdd" / "audit").mkdir(parents=True, exist_ok=True)
    (tmp_path / "bernstein.yaml").write_text(
        "goal: x\nrole_model_policy:\n  developer:\n    base_url: http://10.0.0.5:11434/v1\n    model: m\n",
        encoding="utf-8",
    )
    policy = resolve_effective_policy(SOVEREIGN_PROFILE, load_config_snapshot(tmp_path))
    build_posture_attestation(
        workdir=tmp_path, policy=policy, timestamp=1, chain=AuditChainStore(tmp_path / ".sdd" / "audit")
    )
    with pytest.raises(PostureDriftRefusal) as excinfo:
        _preflight(tmp_path)
    assert excinfo.value.record["violations"]


def test_drift_refusal_is_not_a_spawn_error() -> None:
    """A drift refusal must be a hard stop, not swallowed by provider failover."""
    from bernstein.adapters.base import SpawnError

    assert not issubclass(PostureDriftRefusal, SpawnError)


def test_resume_path_invokes_the_gate() -> None:
    """AC (bypass): spawn_for_resume must run the same drift gate as the main path."""
    import inspect

    source = inspect.getsource(AgentSpawner.spawn_for_resume)
    assert "_preflight_posture_drift" in source


def test_sovereign_rejects_cli_allow_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--allow-network is rejected under sovereign so egress is config-sourced only."""
    import click

    from bernstein.cli.run_bootstrap import _install_profile_network_policy

    for var in ("BERNSTEIN_PROFILE_MODE", "BERNSTEIN_NETWORK_POLICY", ENV_SOVEREIGN_MODE):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(click.UsageError, match="allowed_egress"):
        _install_profile_network_policy(run_profile="sovereign", allow_network=("10.0.0.5:11434",), workdir=tmp_path)


def test_sovereign_egress_sourced_from_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sovereign installs the deny-all-or-allowlist policy from config, then restores."""
    from bernstein.cli.run_bootstrap import _install_profile_network_policy
    from bernstein.core.security.network_policy import ENV_NETWORK_POLICY, ENV_PROFILE_MODE, policy_from_env
    from bernstein.core.security.socket_guard import uninstall_runtime_socket_guard

    for var in (ENV_PROFILE_MODE, ENV_NETWORK_POLICY, ENV_SOVEREIGN_MODE):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / "bernstein.yaml").write_text(
        "goal: x\nsovereign:\n  enabled: true\n  allowed_egress: ['10.0.0.5:11434']\n", encoding="utf-8"
    )
    try:
        _install_profile_network_policy(run_profile="sovereign", allow_network=(), workdir=tmp_path)
        assert is_sovereign_profile() is True
        assert policy_from_env().is_allowed("10.0.0.5", 11434) is True
        assert policy_from_env().is_allowed("api.openai.com", 443) is False
    finally:
        uninstall_runtime_socket_guard()


# ---------------------------------------------------------------------------
# AC5: airgap behavior unchanged when sovereign is not selected
# ---------------------------------------------------------------------------


def test_airgap_helpers_unaffected_by_sovereign_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    from bernstein.core.security.network_policy import ENV_PROFILE_MODE, PROFILE_AIRGAP

    monkeypatch.setenv(ENV_PROFILE_MODE, PROFILE_AIRGAP)
    monkeypatch.delenv(ENV_SOVEREIGN_MODE, raising=False)
    # A bare airgap run is airgap, not sovereign.
    assert is_airgap_profile() is True
    assert is_sovereign_profile() is False


def test_sovereign_marker_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_SOVEREIGN_MODE, "1")
    assert is_sovereign_profile() is True


def test_install_network_policy_sovereign_composes_airgap(monkeypatch: pytest.MonkeyPatch) -> None:
    """--profile sovereign sets the airgap network posture + the sovereign marker."""
    from bernstein.cli.run_bootstrap import _install_network_policy
    from bernstein.core.security.network_policy import (
        ENV_NETWORK_POLICY,
        ENV_PROFILE_MODE,
        PROFILE_AIRGAP,
        policy_from_env,
    )
    from bernstein.core.security.socket_guard import uninstall_runtime_socket_guard

    for var in (ENV_PROFILE_MODE, ENV_NETWORK_POLICY, ENV_SOVEREIGN_MODE):
        monkeypatch.delenv(var, raising=False)
    try:
        _install_network_policy(run_profile="sovereign", allow_network=())
        import os

        assert os.environ[ENV_PROFILE_MODE] == PROFILE_AIRGAP  # network posture = airgap
        assert is_sovereign_profile() is True
        assert policy_from_env().allow_any is False  # deny-all
    finally:
        uninstall_runtime_socket_guard()


def test_install_network_policy_airgap_does_not_set_sovereign(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC5: a plain airgap run never sets the sovereign marker."""
    from bernstein.cli.run_bootstrap import _install_network_policy
    from bernstein.core.security.network_policy import ENV_NETWORK_POLICY, ENV_PROFILE_MODE
    from bernstein.core.security.socket_guard import uninstall_runtime_socket_guard

    for var in (ENV_PROFILE_MODE, ENV_NETWORK_POLICY, ENV_SOVEREIGN_MODE):
        monkeypatch.delenv(var, raising=False)
    try:
        _install_network_policy(run_profile="airgap", allow_network=())
        assert is_sovereign_profile() is False
        assert is_airgap_profile() is True
    finally:
        uninstall_runtime_socket_guard()
