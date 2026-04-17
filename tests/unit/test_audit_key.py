"""Tests for audit-043: HMAC audit key lives outside the audit log directory.

Covers:
    * Auto-generation on first boot (fresh install).
    * Mode-0600 enforcement: world/group-readable keys are rejected.
    * Path override via ``BERNSTEIN_AUDIT_KEY_PATH``.
    * Tamper detection still works when the split key is in use.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
from bernstein.core.audit import (
    AUDIT_KEY_ENV,
    AuditKeyPermissionError,
    AuditLog,
    _default_audit_key_path,  # pyright: ignore[reportPrivateUsage]
    load_or_create_audit_key,
)

pytestmark = pytest.mark.audit_key_real


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip any inherited audit-key env so each test controls resolution."""
    monkeypatch.delenv(AUDIT_KEY_ENV, raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)


def test_key_auto_generated_on_first_boot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """First boot with no pre-existing key must create one with 0600 perms."""
    key_path = tmp_path / "state" / "audit.key"
    monkeypatch.setenv(AUDIT_KEY_ENV, str(key_path))
    assert not key_path.exists()

    key = load_or_create_audit_key()

    assert key_path.exists(), "load_or_create_audit_key did not persist the key"
    assert len(key) == 64, "expected 32-byte hex key (64 chars)"
    mode = stat.S_IMODE(key_path.stat().st_mode)
    assert mode == 0o600, f"new key should be 0600, got {mode:04o}"


def test_key_path_configurable_via_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """BERNSTEIN_AUDIT_KEY_PATH overrides the default XDG location."""
    custom = tmp_path / "custom" / "my-audit.key"
    monkeypatch.setenv(AUDIT_KEY_ENV, str(custom))

    resolved = _default_audit_key_path()
    assert resolved == custom

    load_or_create_audit_key()
    assert custom.exists()


def test_default_path_is_outside_sdd_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Default path must NOT live under ``.sdd/`` — that was the audit-043 bug."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    path = _default_audit_key_path()

    parts = set(path.parts)
    assert ".sdd" not in parts, f"audit key must not live inside .sdd/: {path}"
    assert "audit" not in parts, f"audit key must not live in an audit/ dir: {path}"
    assert path.name == "audit.key"


def test_key_load_rejects_world_readable_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A key file with group or world bits set must fail loading."""
    key_path = tmp_path / "audit.key"
    key_path.write_bytes(b"a" * 64)
    key_path.chmod(0o644)  # world-readable — insecure
    monkeypatch.setenv(AUDIT_KEY_ENV, str(key_path))

    with pytest.raises(AuditKeyPermissionError) as excinfo:
        load_or_create_audit_key()
    assert "insecure permissions" in str(excinfo.value)


def test_key_load_rejects_group_readable_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Group-readable (0640) is rejected — only owner may read the key."""
    key_path = tmp_path / "audit.key"
    key_path.write_bytes(b"a" * 64)
    key_path.chmod(0o640)
    monkeypatch.setenv(AUDIT_KEY_ENV, str(key_path))

    with pytest.raises(AuditKeyPermissionError):
        load_or_create_audit_key()


def test_key_load_accepts_0600(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The canonical 0600 mode loads without error."""
    key_path = tmp_path / "audit.key"
    key_path.write_bytes(b"b" * 64)
    key_path.chmod(0o600)
    monkeypatch.setenv(AUDIT_KEY_ENV, str(key_path))

    loaded = load_or_create_audit_key()
    assert loaded == b"b" * 64


def test_auditlog_uses_split_key_location(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AuditLog constructed without an explicit key reads from the split path —
    NOT from ``<audit_dir>/../config/audit-key``.
    """
    audit_dir = tmp_path / ".sdd" / "audit"
    key_path = tmp_path / "state" / "audit.key"
    monkeypatch.setenv(AUDIT_KEY_ENV, str(key_path))

    log = AuditLog(audit_dir)
    log.log("test.event", "tester", "task", "T-1")

    # Key lives at the env-override path, not next to the log.
    assert key_path.exists()
    legacy = audit_dir.parent / "config" / "audit-key"
    assert not legacy.exists(), "audit-043 regression: key written next to log"


def test_auditlog_rejects_insecure_key_at_boot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AuditLog() must refuse to start on an insecure key file."""
    audit_dir = tmp_path / "audit"
    key_path = tmp_path / "audit.key"
    key_path.write_bytes(b"x" * 64)
    key_path.chmod(0o644)
    monkeypatch.setenv(AUDIT_KEY_ENV, str(key_path))

    with pytest.raises(AuditKeyPermissionError):
        AuditLog(audit_dir)


def test_tampered_log_detected_with_split_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: split key is loaded, events are chained, tamper is detected."""
    audit_dir = tmp_path / ".sdd" / "audit"
    key_path = tmp_path / "state" / "audit.key"
    monkeypatch.setenv(AUDIT_KEY_ENV, str(key_path))

    log = AuditLog(audit_dir)
    log.log("e1", "a1", "r", "i1")
    log.log("e2", "a2", "r", "i2")

    valid, errors = log.verify()
    assert valid, f"chain should verify clean, got errors: {errors}"

    # Tamper with the log file content.
    log_files = sorted(audit_dir.glob("*.jsonl"))
    assert log_files, "no log files written"
    content = log_files[0].read_text()
    tampered = content.replace('"a2"', '"attacker"', 1)
    assert tampered != content, "tamper fixture did not find a field to replace"
    log_files[0].write_text(tampered)

    # Fresh AuditLog reloads the split key and detects tampering.
    fresh = AuditLog(audit_dir)
    valid_after, errors_after = fresh.verify()
    assert valid_after is False
    assert any("HMAC mismatch" in e for e in errors_after)


def test_explicit_key_path_arg_overrides_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Passing ``key_path=`` to ``load_or_create_audit_key`` wins over the env."""
    env_path = tmp_path / "from-env.key"
    arg_path = tmp_path / "from-arg.key"
    monkeypatch.setenv(AUDIT_KEY_ENV, str(env_path))

    load_or_create_audit_key(key_path=arg_path)

    assert arg_path.exists()
    assert not env_path.exists()


def test_auditlog_accepts_key_path_arg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AuditLog(key_path=...) honours the explicit override."""
    audit_dir = tmp_path / "audit"
    explicit = tmp_path / "explicit.key"
    monkeypatch.delenv(AUDIT_KEY_ENV, raising=False)

    AuditLog(audit_dir, key_path=explicit)

    assert explicit.exists()
    mode = stat.S_IMODE(explicit.stat().st_mode)
    assert mode == 0o600


def test_xdg_state_home_used_when_env_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Falling back to ``$XDG_STATE_HOME/bernstein/audit.key`` when override is unset."""
    monkeypatch.delenv(AUDIT_KEY_ENV, raising=False)
    state = tmp_path / "xdg_state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))

    path = _default_audit_key_path()
    assert path == state / "bernstein" / "audit.key"


def test_home_fallback_when_no_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fall back to ``$HOME/.local/state/bernstein/audit.key`` as XDG default."""
    monkeypatch.delenv(AUDIT_KEY_ENV, raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    path = _default_audit_key_path()
    assert path == tmp_path / ".local" / "state" / "bernstein" / "audit.key"
