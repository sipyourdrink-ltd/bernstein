"""Tests for ``agent_card_keystore`` - persistent Ed25519 keypair + rotation."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import datetime as _dt
import os
import stat
import sys
from pathlib import Path

import pytest

from bernstein.core.security.agent_card_keystore import (
    DEFAULT_GRACE_SECONDS,
    AgentCardKeystore,
)

# ---------------------------------------------------------------------------
# First-run generation
# ---------------------------------------------------------------------------


class TestFirstRun:
    def test_creates_pem_pair_on_first_call(self, tmp_path: Path) -> None:
        ks = AgentCardKeystore(tmp_path / "keys")
        priv, pub = ks.load_or_generate()
        assert priv.startswith(b"-----BEGIN PRIVATE KEY-----")
        assert pub.startswith(b"-----BEGIN PUBLIC KEY-----")
        assert (tmp_path / "keys" / "agent-card.ed25519").is_file()
        assert (tmp_path / "keys" / "agent-card.ed25519.pub").is_file()

    def test_private_file_is_owner_only_0600(self, tmp_path: Path) -> None:
        """Persistent private key must be ``0o600`` (owner-only).

        Anything wider leaks the signing key to any local user - the
        keystore explicitly enforces the permission bits both at
        generation time and on every load (POSIX systems).
        """
        ks = AgentCardKeystore(tmp_path / "keys")
        ks.load_or_generate()

        priv = tmp_path / "keys" / "agent-card.ed25519"
        mode = priv.stat().st_mode & 0o777
        if sys.platform != "win32":
            assert mode == 0o600, f"private key permissions are {oct(mode)}, expected 0o600"

    def test_uses_o_excl_so_two_processes_cannot_clobber(self, tmp_path: Path) -> None:
        """Concurrent first-run callers must not race over the same file.

        We simulate the race by pre-creating an ``O_EXCL``-occupied file -
        the keystore must refuse to clobber it and instead read the
        existing key on the next call.
        """
        ks = AgentCardKeystore(tmp_path / "keys")
        priv_path = tmp_path / "keys" / "agent-card.ed25519"
        pub_path = tmp_path / "keys" / "agent-card.ed25519.pub"
        priv_path.parent.mkdir(parents=True)
        # First writer wins.
        priv_a, pub_a = ks.load_or_generate()
        # A second keystore bound to the same dir reads the same bytes
        # rather than overwriting.
        ks2 = AgentCardKeystore(tmp_path / "keys")
        priv_b, pub_b = ks2.load_or_generate()
        assert priv_a == priv_b
        assert pub_a == pub_b
        assert priv_path.exists()
        assert pub_path.exists()

    def test_load_refuses_unsafe_permissions(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A private file with group/world perms must raise on load on POSIX."""
        ks = AgentCardKeystore(tmp_path / "keys")
        ks.load_or_generate()
        priv = tmp_path / "keys" / "agent-card.ed25519"
        os.chmod(priv, 0o644)
        monkeypatch.setattr("sys.platform", "linux")

        real_stat = os.stat

        def _mock_stat(path: os.PathLike[str] | str | bytes | int, *args: object, **kwargs: object) -> os.stat_result:
            st = real_stat(path, *args, **kwargs)
            if str(path).endswith("agent-card.ed25519"):
                # Ensure group/other bits are set for the test
                return os.stat_result(
                    (
                        st.st_mode | 0o044,
                        st.st_ino,
                        st.st_dev,
                        st.st_nlink,
                        st.st_uid,
                        st.st_gid,
                        st.st_size,
                        st.st_atime,
                        st.st_mtime,
                        st.st_ctime,
                    )
                )
            return st

        monkeypatch.setattr("os.stat", _mock_stat)

        ks2 = AgentCardKeystore(tmp_path / "keys")
        with pytest.raises(PermissionError, match="unsafe permissions"):
            ks2.load_or_generate()

    def test_load_permits_windows_permissions(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """On Windows (non-POSIX), unsafe-looking mode bits do not raise PermissionError (#4765)."""
        ks = AgentCardKeystore(tmp_path / "keys")
        priv_a, pub_a = ks.load_or_generate()
        monkeypatch.setattr("sys.platform", "win32")

        real_stat = os.stat

        def _mock_stat(path: os.PathLike[str] | str | bytes | int, *args: object, **kwargs: object) -> os.stat_result:
            st = real_stat(path, *args, **kwargs)
            if str(path).endswith("agent-card.ed25519"):
                # Permissive-looking bits from Windows ACL representation
                return os.stat_result(
                    (
                        st.st_mode | 0o066,
                        st.st_ino,
                        st.st_dev,
                        st.st_nlink,
                        st.st_uid,
                        st.st_gid,
                        st.st_size,
                        st.st_atime,
                        st.st_mtime,
                        st.st_ctime,
                    )
                )
            return st

        monkeypatch.setattr("os.stat", _mock_stat)

        ks2 = AgentCardKeystore(tmp_path / "keys")
        priv_b, pub_b = ks2.load_or_generate()
        assert priv_a == priv_b
        assert pub_a == pub_b


# ---------------------------------------------------------------------------
# Atomic re-load
# ---------------------------------------------------------------------------


class TestAtomicReload:
    def test_second_call_returns_same_keypair(self, tmp_path: Path) -> None:
        """Subsequent ``load_or_generate`` calls must reuse the on-disk key."""
        ks = AgentCardKeystore(tmp_path / "keys")
        priv_a, pub_a = ks.load_or_generate()
        priv_b, pub_b = ks.load_or_generate()
        assert priv_a == priv_b
        assert pub_a == pub_b

    def test_separate_keystore_instance_reads_same_keypair(self, tmp_path: Path) -> None:
        """Restarting the orchestrator (new keystore instance) must reuse the key."""
        ks_a = AgentCardKeystore(tmp_path / "keys")
        priv_a, pub_a = ks_a.load_or_generate()

        ks_b = AgentCardKeystore(tmp_path / "keys")
        priv_b, pub_b = ks_b.load_or_generate()
        assert priv_a == priv_b
        assert pub_a == pub_b


# ---------------------------------------------------------------------------
# Rotation + grace window
# ---------------------------------------------------------------------------


class _FrozenClock:
    """Datetime stand-in driven by the test, so rotation timestamps are stable."""

    instant: _dt.datetime

    def __init__(self, instant: _dt.datetime) -> None:
        self.instant = instant

    @classmethod
    def now(cls, tz: _dt.tzinfo | None = None) -> _dt.datetime:
        return cls.instant


class TestRotation:
    def test_rotate_archives_previous_and_mints_new(self, tmp_path: Path) -> None:
        ks = AgentCardKeystore(tmp_path / "keys")
        priv_a, pub_a = ks.load_or_generate()

        priv_b, pub_b = ks.rotate()
        assert priv_a != priv_b, "rotation must produce a fresh private key"
        assert pub_a != pub_b, "rotation must produce a fresh public key"
        archive_dir = tmp_path / "keys" / "archive"
        assert archive_dir.is_dir()
        # One archive entry per rotation.
        archived = sorted(archive_dir.iterdir())
        assert len(archived) == 1
        assert (archived[0] / "agent-card.ed25519.pub").is_file()
        assert (archived[0] / "rotated_at.txt").is_file()

    def test_jwks_grace_window_includes_archived_key(self, tmp_path: Path) -> None:
        """An archived key minted within the grace window stays in JWKS.

        Verifiers cached on the previous JWKS keep validating until their
        HTTP cache (max-age=3600 on the agent.json route) ages out and they
        refetch the fresh keys list.
        """
        clock_class = type(
            "_PinnedClock",
            (_FrozenClock,),
            {"instant": _dt.datetime(2026, 5, 1, 12, 0, 0, tzinfo=_dt.UTC)},
        )
        ks = AgentCardKeystore(tmp_path / "keys", clock=clock_class)
        ks.load_or_generate()
        ks.rotate()

        archived = ks.list_archived()
        assert len(archived) == 1
        # The archived ``kid`` carries the rotation timestamp so the JWKS
        # entry is unambiguous.
        assert archived[0].kid.startswith("agent-bernstein-orchestrator-")
        # Public key must round-trip through PEM.
        assert archived[0].public_pem.startswith(b"-----BEGIN PUBLIC KEY-----")

    def test_archived_key_drops_after_grace_window(self, tmp_path: Path) -> None:
        """An archived key whose ``rotated_at`` is older than the grace window
        must drop out of the JWKS so verifiers stop trusting it."""
        rotation_time = _dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=_dt.UTC)

        rotation_clock = type("_C1", (_FrozenClock,), {"instant": rotation_time})
        ks_rotate = AgentCardKeystore(tmp_path / "keys", clock=rotation_clock)
        ks_rotate.load_or_generate()
        ks_rotate.rotate()

        # Bind a second keystore whose clock is 25h past the rotation -
        # outside the default 24h grace window.
        future = rotation_time + _dt.timedelta(seconds=DEFAULT_GRACE_SECONDS + 3600)
        future_clock = type("_C2", (_FrozenClock,), {"instant": future})
        ks_after = AgentCardKeystore(tmp_path / "keys", clock=future_clock)
        assert ks_after.list_archived() == []

    def test_rotated_private_key_keeps_0600_permissions(self, tmp_path: Path) -> None:
        """The freshly-minted post-rotation private must be 0600 too."""
        ks = AgentCardKeystore(tmp_path / "keys")
        ks.load_or_generate()
        ks.rotate()
        priv = tmp_path / "keys" / "agent-card.ed25519"
        mode = priv.stat().st_mode & 0o777
        if sys.platform != "win32":
            assert mode == 0o600

        # Archived private should also retain restrictive bits.
        archived_priv = next((tmp_path / "keys" / "archive").iterdir()) / "agent-card.ed25519"
        archived_mode = stat.S_IMODE(archived_priv.stat().st_mode)
        if sys.platform != "win32":
            assert archived_mode == 0o600
