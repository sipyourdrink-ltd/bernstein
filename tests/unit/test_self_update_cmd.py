"""Tests for the provenance-verified `bernstein self` update surface (#2942).

Every test is hermetic: the release feed is a signed document written into
``tmp_path``, ``Path.home()`` is redirected there, and pip is always a stub.
No test opens a socket; several assert that positively.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from bernstein.cli.self_update_cmd import (
    _active_run_blockers,
    _get_installed_version,
    cached_advisory_summary,
    check_update_cmd,
    pin_cmd,
    rollback_cmd,
    self_group,
    self_update_cmd,
    update_cmd,
)
from click.testing import CliRunner

from bernstein.core.distribution.update_advisory import (
    ENV_RELEASE_FEED,
    ENV_TRUST_ROOT,
    ReleaseEntry,
    build_install_receipt,
    build_release_feed_document,
    store_cached_feed,
    store_receipt,
)
from bernstein.core.security.agent_card_signer import generate_ed25519_keypair

if TYPE_CHECKING:
    from collections.abc import Iterator

_WHEEL_BODY = b"a plausible wheel payload"
_WHEEL_SHA256 = hashlib.sha256(_WHEEL_BODY).hexdigest()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Redirect every ``~/.bernstein`` write into a throwaway directory."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: fake_home))
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(fake_home / "audit.key"))
    monkeypatch.delenv("BERNSTEIN_CREDENTIAL_SIGNING_KEY", raising=False)
    monkeypatch.delenv("BERNSTEIN_PROFILE_MODE", raising=False)
    monkeypatch.delenv("BERNSTEIN_NETWORK_POLICY", raising=False)
    yield fake_home


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / ".sdd").mkdir(parents=True)
    return root


@pytest.fixture
def signing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[bytes, str, Path]:
    """Return ``(private_pem, public_pem, trust_root_path)`` and install the root."""
    private_pem, public_pem_bytes = generate_ed25519_keypair()
    public_pem = public_pem_bytes.decode("ascii")
    trust_root = tmp_path / "release-trust-root.pem"
    trust_root.write_text(public_pem)
    monkeypatch.setenv(ENV_TRUST_ROOT, str(trust_root))
    return private_pem, public_pem, trust_root


def _entry(version: str, surface: str, digest: str) -> ReleaseEntry:
    return ReleaseEntry(
        version=version,
        wheel_name=f"bernstein-{version}-py3-none-any.whl",
        wheel_sha256=digest,
        surface=surface,
        released_at="2026-07-01T00:00:00Z",
    )


def _write_feed(
    tmp_path: Path,
    signing: tuple[bytes, str, Path],
    monkeypatch: pytest.MonkeyPatch,
    *,
    entries: list[ReleaseEntry] | None = None,
    tamper: bool = False,
) -> Path:
    private_pem, public_pem, _root = signing
    releases = entries or [
        _entry("3.10.0", "feature", "b" * 64),
        _entry("3.12.0", "verification", _WHEEL_SHA256),
    ]
    document: dict[str, Any] = build_release_feed_document(
        releases,
        private_key_pem=private_pem,
        public_key_pem=public_pem,
        generated_at="2026-07-01T00:00:00Z",
    )
    if tamper:
        document["feed"]["releases"][-1]["wheel_sha256"] = "0" * 64
    path = tmp_path / "release-feed.json"
    path.write_text(json.dumps(document))
    monkeypatch.setenv(ENV_RELEASE_FEED, str(path))
    return path


@pytest.fixture
def no_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if anything opens an HTTP connection."""
    import urllib.request

    def _explode(*_args: object, **_kwargs: object) -> None:
        pytest.fail("the update surface opened a socket")

    monkeypatch.setattr(urllib.request, "urlopen", _explode)


@pytest.fixture
def quiet_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend no run is in flight."""
    monkeypatch.setattr("bernstein.cli.commands.self_update_cmd._active_run_blockers", lambda _root: [])


@pytest.fixture
def stub_sigstore(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend the ``gh`` CLI is absent so the attestation pass reports a skip."""
    monkeypatch.setattr(
        "bernstein.core.distribution.sigstore_attestation_verify.shutil.which",
        lambda _name: None,
    )


def _runner() -> CliRunner:
    return CliRunner()


# ---------------------------------------------------------------------------
# Group wiring
# ---------------------------------------------------------------------------


class TestGroupWiring:
    def test_subcommands_registered(self) -> None:
        assert sorted(self_group.commands) == ["check-update", "pin", "rollback", "unpin", "update"]

    def test_installed_version_is_a_string(self) -> None:
        assert isinstance(_get_installed_version(), str)


# ---------------------------------------------------------------------------
# check-update
# ---------------------------------------------------------------------------


class TestCheckUpdate:
    def test_verified_feed_yields_a_sealed_advisory(
        self,
        tmp_path: Path,
        home: Path,
        workdir: Path,
        signing: tuple[bytes, str, Path],
        monkeypatch: pytest.MonkeyPatch,
        no_sockets: None,
    ) -> None:
        _write_feed(tmp_path, signing, monkeypatch)
        with patch("bernstein.cli.commands.self_update_cmd._get_installed_version", return_value="3.9.0"):
            result = _runner().invoke(check_update_cmd, ["--workdir", str(workdir), "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        advisory = payload["advisory"]["advisory"]
        assert advisory["candidate_version"] == "3.12.0"
        assert advisory["provenance_verified"] is True
        assert advisory["surface_delta"]["highest"] == "verification"
        assert (home / ".bernstein" / "update-advisory.json").exists()

    def test_advisory_is_anchored_into_the_audit_chain(
        self,
        tmp_path: Path,
        home: Path,
        workdir: Path,
        signing: tuple[bytes, str, Path],
        monkeypatch: pytest.MonkeyPatch,
        no_sockets: None,
    ) -> None:
        from bernstein.core.security.audit_chain import EVENT_UPDATE_ADVISORY, AuditChainStore

        _write_feed(tmp_path, signing, monkeypatch)
        with patch("bernstein.cli.commands.self_update_cmd._get_installed_version", return_value="3.9.0"):
            result = _runner().invoke(check_update_cmd, ["--workdir", str(workdir), "--json"])
        assert result.exit_code == 0, result.output
        rows = AuditChainStore(workdir / ".sdd" / "audit").query(event_type=EVENT_UPDATE_ADVISORY)
        assert len(rows) == 1
        assert rows[0].details["candidate_version"] == "3.12.0"
        assert "prev_chain_digest" in rows[0].details

    def test_tampered_feed_is_refused_not_surfaced(
        self,
        tmp_path: Path,
        home: Path,
        workdir: Path,
        signing: tuple[bytes, str, Path],
        monkeypatch: pytest.MonkeyPatch,
        no_sockets: None,
    ) -> None:
        _write_feed(tmp_path, signing, monkeypatch, tamper=True)
        result = _runner().invoke(check_update_cmd, ["--workdir", str(workdir)])
        assert result.exit_code != 0
        assert "Refusing" in result.output
        assert not (home / ".bernstein" / "update-advisory.json").exists()

    def test_no_trust_root_fails_closed(
        self,
        tmp_path: Path,
        home: Path,
        workdir: Path,
        signing: tuple[bytes, str, Path],
        monkeypatch: pytest.MonkeyPatch,
        no_sockets: None,
    ) -> None:
        _write_feed(tmp_path, signing, monkeypatch)
        monkeypatch.delenv(ENV_TRUST_ROOT, raising=False)
        result = _runner().invoke(check_update_cmd, ["--workdir", str(workdir)])
        assert result.exit_code != 0
        assert "trust root" in result.output

    def test_no_feed_configured_is_refused(
        self,
        home: Path,
        workdir: Path,
        signing: tuple[bytes, str, Path],
        monkeypatch: pytest.MonkeyPatch,
        no_sockets: None,
    ) -> None:
        monkeypatch.delenv(ENV_RELEASE_FEED, raising=False)
        result = _runner().invoke(check_update_cmd, ["--workdir", str(workdir)])
        assert result.exit_code != 0
        assert "release feed" in result.output

    def test_airgap_profile_refuses_a_remote_feed(
        self,
        home: Path,
        workdir: Path,
        signing: tuple[bytes, str, Path],
        monkeypatch: pytest.MonkeyPatch,
        no_sockets: None,
    ) -> None:
        monkeypatch.setenv("BERNSTEIN_PROFILE_MODE", "airgap")
        result = _runner().invoke(
            check_update_cmd,
            ["--workdir", str(workdir), "--feed", "https://example.invalid/feed.json"],
        )
        assert result.exit_code != 0
        assert "air-gap" in result.output

    def test_airgap_profile_still_reads_a_mirrored_feed(
        self,
        tmp_path: Path,
        home: Path,
        workdir: Path,
        signing: tuple[bytes, str, Path],
        monkeypatch: pytest.MonkeyPatch,
        no_sockets: None,
    ) -> None:
        feed_path = _write_feed(tmp_path, signing, monkeypatch)
        monkeypatch.setenv("BERNSTEIN_PROFILE_MODE", "airgap")
        with patch("bernstein.cli.commands.self_update_cmd._get_installed_version", return_value="3.9.0"):
            result = _runner().invoke(
                check_update_cmd,
                ["--workdir", str(workdir), "--feed", str(feed_path), "--json"],
            )
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["advisory"]["advisory"]["offline_profile"] is True

    def test_up_to_date(
        self,
        tmp_path: Path,
        home: Path,
        workdir: Path,
        signing: tuple[bytes, str, Path],
        monkeypatch: pytest.MonkeyPatch,
        no_sockets: None,
    ) -> None:
        _write_feed(tmp_path, signing, monkeypatch)
        with patch("bernstein.cli.commands.self_update_cmd._get_installed_version", return_value="9.9.9"):
            result = _runner().invoke(check_update_cmd, ["--workdir", str(workdir), "--json"])
        assert result.exit_code == 0
        assert json.loads(result.output)["advisory"]["advisory"]["candidate_version"] is None


class TestOfflineVerify:
    def _seal(
        self,
        tmp_path: Path,
        home: Path,
        workdir: Path,
        signing: tuple[bytes, str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> dict[str, Any]:
        _write_feed(tmp_path, signing, monkeypatch)
        with patch("bernstein.cli.commands.self_update_cmd._get_installed_version", return_value="3.9.0"):
            result = _runner().invoke(check_update_cmd, ["--workdir", str(workdir), "--json"])
        assert result.exit_code == 0, result.output
        return json.loads(result.output)["advisory"]

    def test_verify_recomputes_offline(
        self,
        tmp_path: Path,
        home: Path,
        workdir: Path,
        signing: tuple[bytes, str, Path],
        monkeypatch: pytest.MonkeyPatch,
        no_sockets: None,
    ) -> None:
        sealed = self._seal(tmp_path, home, workdir, signing, monkeypatch)
        path = tmp_path / "advisory.json"
        path.write_text(json.dumps(sealed))
        result = _runner().invoke(check_update_cmd, ["--verify", str(path), "--json"])
        assert result.exit_code == 0
        assert json.loads(result.output)["ok"] is True

    def test_verify_rejects_a_stripped_signature(
        self,
        tmp_path: Path,
        home: Path,
        workdir: Path,
        signing: tuple[bytes, str, Path],
        monkeypatch: pytest.MonkeyPatch,
        no_sockets: None,
    ) -> None:
        sealed = self._seal(tmp_path, home, workdir, signing, monkeypatch)
        sealed.pop("signature")
        path = tmp_path / "advisory.json"
        path.write_text(json.dumps(sealed))
        result = _runner().invoke(check_update_cmd, ["--verify", str(path), "--json"])
        assert result.exit_code != 0
        assert json.loads(result.output)["ok"] is False

    def test_verify_rejects_a_blanked_chain_anchor(
        self,
        tmp_path: Path,
        home: Path,
        workdir: Path,
        signing: tuple[bytes, str, Path],
        monkeypatch: pytest.MonkeyPatch,
        no_sockets: None,
    ) -> None:
        sealed = self._seal(tmp_path, home, workdir, signing, monkeypatch)
        sealed["advisory"]["checked_at_chain_anchor"] = ""
        path = tmp_path / "advisory.json"
        path.write_text(json.dumps(sealed))
        result = _runner().invoke(check_update_cmd, ["--verify", str(path), "--json"])
        assert result.exit_code != 0

    def test_cached_view_never_touches_the_network(
        self,
        tmp_path: Path,
        home: Path,
        workdir: Path,
        signing: tuple[bytes, str, Path],
        monkeypatch: pytest.MonkeyPatch,
        no_sockets: None,
    ) -> None:
        self._seal(tmp_path, home, workdir, signing, monkeypatch)
        monkeypatch.delenv(ENV_RELEASE_FEED, raising=False)
        result = _runner().invoke(check_update_cmd, ["--cached", "--json"])
        assert result.exit_code == 0
        assert json.loads(result.output)["ok"] is True

    def test_cached_view_without_a_cache(self, home: Path, no_sockets: None) -> None:
        result = _runner().invoke(check_update_cmd, ["--cached"])
        assert result.exit_code != 0
        assert "No cached update advisory" in result.output

    def test_summary_helper_is_local_only(
        self,
        tmp_path: Path,
        home: Path,
        workdir: Path,
        signing: tuple[bytes, str, Path],
        monkeypatch: pytest.MonkeyPatch,
        no_sockets: None,
    ) -> None:
        assert cached_advisory_summary() is None
        self._seal(tmp_path, home, workdir, signing, monkeypatch)
        summary = cached_advisory_summary()
        assert summary is not None
        assert summary["verified"] is True
        assert summary["candidate_version"] == "3.12.0"
        assert summary["surface"] == "verification"


# ---------------------------------------------------------------------------
# update: the guards
# ---------------------------------------------------------------------------


class TestUpdateGuards:
    def test_refuses_while_a_run_is_active(
        self,
        tmp_path: Path,
        home: Path,
        workdir: Path,
        signing: tuple[bytes, str, Path],
        monkeypatch: pytest.MonkeyPatch,
        no_sockets: None,
    ) -> None:
        _write_feed(tmp_path, signing, monkeypatch)
        monkeypatch.setattr(
            "bernstein.cli.commands.self_update_cmd._active_run_blockers",
            lambda _root: ["detached run r1 is live (supervisor pid 4242)"],
        )
        with patch("bernstein.cli.commands.self_update_cmd._pip") as mock_pip:
            result = _runner().invoke(update_cmd, ["--workdir", str(workdir), "--yes"])
        assert result.exit_code != 0
        assert "in flight" in result.output
        mock_pip.assert_not_called()

    def test_unreadable_run_state_fails_closed(self, workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(_root: Path) -> None:
            raise OSError("run store unreadable")

        monkeypatch.setattr("bernstein.core.run_service.paths.list_run_ids", _boom)
        blockers = _active_run_blockers(workdir)
        assert any("refusing to update blind" in line for line in blockers)

    def test_quiet_project_has_no_blockers(self, workdir: Path) -> None:
        assert _active_run_blockers(workdir) == []

    def test_pin_blocks_the_upgrade(
        self,
        tmp_path: Path,
        home: Path,
        workdir: Path,
        signing: tuple[bytes, str, Path],
        monkeypatch: pytest.MonkeyPatch,
        quiet_runs: None,
        no_sockets: None,
    ) -> None:
        _write_feed(tmp_path, signing, monkeypatch)
        pin_result = _runner().invoke(pin_cmd, ["3.10.0", "--workdir", str(workdir)])
        assert pin_result.exit_code == 0, pin_result.output
        with (
            patch("bernstein.cli.commands.self_update_cmd._get_installed_version", return_value="3.9.0"),
            patch("bernstein.cli.commands.self_update_cmd._pip") as mock_pip,
        ):
            result = _runner().invoke(update_cmd, ["--workdir", str(workdir)], input="n\n")
        assert result.exit_code == 0
        # The pin caps the candidate at 3.10.0 rather than pushing 3.12.0.
        assert "3.10.0" in result.output
        assert "3.12.0" not in result.output
        mock_pip.assert_not_called()

    def test_pin_crossing_needs_the_override(
        self,
        tmp_path: Path,
        home: Path,
        workdir: Path,
        signing: tuple[bytes, str, Path],
        monkeypatch: pytest.MonkeyPatch,
        quiet_runs: None,
        no_sockets: None,
    ) -> None:
        _write_feed(
            tmp_path,
            signing,
            monkeypatch,
            entries=[_entry("3.12.0", "security", _WHEEL_SHA256)],
        )
        assert _runner().invoke(pin_cmd, ["3.10.0", "--workdir", str(workdir)]).exit_code == 0
        with (
            patch("bernstein.cli.commands.self_update_cmd._get_installed_version", return_value="3.9.0"),
            patch("bernstein.cli.commands.self_update_cmd._pip") as mock_pip,
        ):
            result = _runner().invoke(update_cmd, ["--workdir", str(workdir), "--yes"])
        assert result.exit_code == 0
        assert "up to date" in result.output.lower()
        mock_pip.assert_not_called()

    def test_unpin_clears_the_pin(
        self,
        home: Path,
        workdir: Path,
        signing: tuple[bytes, str, Path],
    ) -> None:
        assert _runner().invoke(pin_cmd, ["3.10.0", "--workdir", str(workdir)]).exit_code == 0
        result = _runner().invoke(self_group, ["unpin"])
        assert result.exit_code == 0
        assert "Removed" in result.output
        again = _runner().invoke(self_group, ["unpin"])
        assert "No version pin" in again.output


# ---------------------------------------------------------------------------
# update: hash verification before install
# ---------------------------------------------------------------------------


class TestUpdateInstall:
    def _stage_wheel(self, body: bytes) -> Any:
        def _download(_version: str, dest: Path) -> Path:
            wheel = dest / "bernstein-3.12.0-py3-none-any.whl"
            wheel.write_bytes(body)
            return wheel

        return _download

    def test_hash_mismatch_aborts_before_pip(
        self,
        tmp_path: Path,
        home: Path,
        workdir: Path,
        signing: tuple[bytes, str, Path],
        monkeypatch: pytest.MonkeyPatch,
        quiet_runs: None,
        stub_sigstore: None,
        no_sockets: None,
    ) -> None:
        _write_feed(tmp_path, signing, monkeypatch)
        monkeypatch.setattr(
            "bernstein.cli.commands.self_update_cmd._download_wheel",
            self._stage_wheel(b"a substituted wheel"),
        )
        with (
            patch("bernstein.cli.commands.self_update_cmd._get_installed_version", return_value="3.9.0"),
            patch("bernstein.cli.commands.self_update_cmd._pip") as mock_pip,
        ):
            result = _runner().invoke(update_cmd, ["--workdir", str(workdir), "--yes"])
        assert result.exit_code != 0
        assert "mismatch" in result.output
        mock_pip.assert_not_called()

    def test_verified_wheel_installs_and_receipts(
        self,
        tmp_path: Path,
        home: Path,
        workdir: Path,
        signing: tuple[bytes, str, Path],
        monkeypatch: pytest.MonkeyPatch,
        quiet_runs: None,
        stub_sigstore: None,
        no_sockets: None,
    ) -> None:
        from bernstein.core.security.audit_chain import EVENT_SELF_UPDATE, AuditChainStore

        _write_feed(tmp_path, signing, monkeypatch)
        monkeypatch.setattr(
            "bernstein.cli.commands.self_update_cmd._download_wheel",
            self._stage_wheel(_WHEEL_BODY),
        )
        with (
            patch("bernstein.cli.commands.self_update_cmd._get_installed_version", return_value="3.9.0"),
            patch("bernstein.cli.commands.self_update_cmd._pip", return_value=(True, "")) as mock_pip,
        ):
            result = _runner().invoke(update_cmd, ["--workdir", str(workdir), "--yes"])
        assert result.exit_code == 0, result.output
        assert "Upgraded to" in result.output
        assert mock_pip.call_count == 1
        assert mock_pip.call_args.args[0][0] == "install"

        rows = AuditChainStore(workdir / ".sdd" / "audit").query(event_type=EVENT_SELF_UPDATE)
        assert len(rows) == 1
        assert rows[0].details["direction"] == "install"
        assert rows[0].details["from_version"] == "3.9.0"
        assert rows[0].details["to_version"] == "3.12.0"
        assert rows[0].details["wheel_sha256"] == _WHEEL_SHA256
        assert list((home / ".bernstein" / "update-receipts").glob("*.json"))

    def test_prompt_cancel_skips_install(
        self,
        tmp_path: Path,
        home: Path,
        workdir: Path,
        signing: tuple[bytes, str, Path],
        monkeypatch: pytest.MonkeyPatch,
        quiet_runs: None,
        no_sockets: None,
    ) -> None:
        _write_feed(tmp_path, signing, monkeypatch)
        with (
            patch("bernstein.cli.commands.self_update_cmd._get_installed_version", return_value="3.9.0"),
            patch("bernstein.cli.commands.self_update_cmd._pip") as mock_pip,
        ):
            result = _runner().invoke(update_cmd, ["--workdir", str(workdir)], input="n\n")
        assert result.exit_code == 0
        assert "cancelled" in result.output.lower()
        mock_pip.assert_not_called()

    def test_pip_failure_exits_nonzero(
        self,
        tmp_path: Path,
        home: Path,
        workdir: Path,
        signing: tuple[bytes, str, Path],
        monkeypatch: pytest.MonkeyPatch,
        quiet_runs: None,
        stub_sigstore: None,
        no_sockets: None,
    ) -> None:
        _write_feed(tmp_path, signing, monkeypatch)
        monkeypatch.setattr(
            "bernstein.cli.commands.self_update_cmd._download_wheel",
            self._stage_wheel(_WHEEL_BODY),
        )
        with (
            patch("bernstein.cli.commands.self_update_cmd._get_installed_version", return_value="3.9.0"),
            patch("bernstein.cli.commands.self_update_cmd._pip", return_value=(False, "boom")),
        ):
            result = _runner().invoke(update_cmd, ["--workdir", str(workdir), "--yes"])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# rollback
# ---------------------------------------------------------------------------


class TestRollback:
    def test_refuses_without_a_receipted_predecessor(
        self,
        home: Path,
        workdir: Path,
        signing: tuple[bytes, str, Path],
        quiet_runs: None,
        no_sockets: None,
    ) -> None:
        result = _runner().invoke(rollback_cmd, ["--workdir", str(workdir), "--yes"])
        assert result.exit_code != 0
        assert "receipted predecessor" in result.output

    def test_refuses_when_the_target_is_not_in_the_verified_feed(
        self,
        tmp_path: Path,
        home: Path,
        workdir: Path,
        signing: tuple[bytes, str, Path],
        monkeypatch: pytest.MonkeyPatch,
        quiet_runs: None,
        no_sockets: None,
    ) -> None:
        private_pem, public_pem, _root = signing
        store_receipt(
            build_install_receipt(
                from_version="2.0.0",
                to_version="3.12.0",
                wheel_sha256=_WHEEL_SHA256,
                provenance_key_fingerprint="sha256:aaa",
                advisory_sha256_value="x",
                direction="install",
                chain_anchor="anchor",
                attestation_ok=None,
                generated_at="2026-07-01T00:00:00Z",
            ),
        )
        store_cached_feed(
            build_release_feed_document(
                [_entry("3.12.0", "verification", _WHEEL_SHA256)],
                private_key_pem=private_pem,
                public_key_pem=public_pem,
                generated_at="2026-07-01T00:00:00Z",
            ),
        )
        result = _runner().invoke(rollback_cmd, ["--workdir", str(workdir), "--yes"])
        assert result.exit_code != 0
        assert "no entry for 2.0.0" in result.output

    def test_rolls_back_to_the_receipted_version(
        self,
        tmp_path: Path,
        home: Path,
        workdir: Path,
        signing: tuple[bytes, str, Path],
        monkeypatch: pytest.MonkeyPatch,
        quiet_runs: None,
        stub_sigstore: None,
        no_sockets: None,
    ) -> None:
        from bernstein.core.security.audit_chain import EVENT_SELF_UPDATE, AuditChainStore

        private_pem, public_pem, _root = signing
        store_receipt(
            build_install_receipt(
                from_version="3.10.0",
                to_version="3.12.0",
                wheel_sha256=_WHEEL_SHA256,
                provenance_key_fingerprint="sha256:aaa",
                advisory_sha256_value="x",
                direction="install",
                chain_anchor="anchor",
                attestation_ok=None,
                generated_at="2026-07-01T00:00:00Z",
            ),
        )
        store_cached_feed(
            build_release_feed_document(
                [_entry("3.10.0", "feature", _WHEEL_SHA256)],
                private_key_pem=private_pem,
                public_key_pem=public_pem,
                generated_at="2026-07-01T00:00:00Z",
            ),
        )

        def _download(_version: str, dest: Path) -> Path:
            wheel = dest / "bernstein-3.10.0-py3-none-any.whl"
            wheel.write_bytes(_WHEEL_BODY)
            return wheel

        monkeypatch.setattr("bernstein.cli.commands.self_update_cmd._download_wheel", _download)
        with (
            patch("bernstein.cli.commands.self_update_cmd._get_installed_version", return_value="3.12.0"),
            patch("bernstein.cli.commands.self_update_cmd._pip", return_value=(True, "")),
        ):
            result = _runner().invoke(rollback_cmd, ["--workdir", str(workdir), "--yes"])
        assert result.exit_code == 0, result.output
        assert "Rolled back to" in result.output
        rows = AuditChainStore(workdir / ".sdd" / "audit").query(event_type=EVENT_SELF_UPDATE)
        assert rows[-1].details["direction"] == "rollback"
        assert rows[-1].details["to_version"] == "3.10.0"
        assert rows[-1].details["wheel_sha256"] == _WHEEL_SHA256
        # The rollback pins the same trust root a forward install would.
        assert str(rows[-1].details["provenance_key_fingerprint"]).startswith("sha256:")

    def test_refuses_while_a_run_is_active(
        self,
        home: Path,
        workdir: Path,
        signing: tuple[bytes, str, Path],
        monkeypatch: pytest.MonkeyPatch,
        no_sockets: None,
    ) -> None:
        monkeypatch.setattr(
            "bernstein.cli.commands.self_update_cmd._active_run_blockers",
            lambda _root: ["spawner is running (pid 7)"],
        )
        result = _runner().invoke(rollback_cmd, ["--workdir", str(workdir), "--yes"])
        assert result.exit_code != 0
        assert "roll back" in result.output.lower()


# ---------------------------------------------------------------------------
# Compatibility alias
# ---------------------------------------------------------------------------


class TestCompatibilityAlias:
    def test_check_flag_dispatches_to_check_update(
        self,
        tmp_path: Path,
        home: Path,
        signing: tuple[bytes, str, Path],
        monkeypatch: pytest.MonkeyPatch,
        no_sockets: None,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _write_feed(tmp_path, signing, monkeypatch)
        with patch("bernstein.cli.commands.self_update_cmd._get_installed_version", return_value="3.9.0"):
            result = _runner().invoke(self_update_cmd, ["--check"])
        assert result.exit_code == 0, result.output
        assert "3.12.0" in result.output

    def test_rollback_flag_dispatches_to_rollback(
        self,
        tmp_path: Path,
        home: Path,
        signing: tuple[bytes, str, Path],
        monkeypatch: pytest.MonkeyPatch,
        quiet_runs: None,
        no_sockets: None,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        result = _runner().invoke(self_update_cmd, ["--rollback", "--yes"])
        assert result.exit_code != 0
        assert "receipted predecessor" in result.output
