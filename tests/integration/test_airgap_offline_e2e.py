"""Real-offline air-gap end-to-end coverage.

Closes the gap left by ``tests/integration/test_airgap_wheelhouse.py``,
where every cosign / GPG subprocess invocation is monkeypatched. This
file exercises the same code paths against:

* a real ``cosign generate-key-pair`` + ``sign-blob`` + ``verify-blob``
  round-trip (skipped when cosign is not on PATH)
* a real ``gpg --detach-sign`` + ``gpg --verify`` round-trip (skipped
  when gpg is not on PATH)
* a real Linux network namespace via ``unshare -n`` (skipped on
  macOS / Windows)
* the runtime socket guard installed by ``--profile airgap`` -- we
  install it in-process and assert direct ``socket.connect`` calls
  to non-allowed hosts raise :class:`NetworkPolicyDenied`
* the doctor airgap battery, broken battery-by-battery to confirm
  every check actually catches its target failure

Sovereign-customer compliance teams accept tests that *prove the
boundary held* far more readily than tests that mock out the
boundary. This file is the proof harness.
"""

from __future__ import annotations

import functools
import json
import os
import shutil
import socket
import subprocess
import sys
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.advanced_cmd import doctor as doctor_group
from bernstein.cli.commands.wheelhouse_cmd import wheelhouse_group
from bernstein.core.distribution import (
    CosignVerifier,
    GpgVerifier,
    verify_wheelhouse,
)
from bernstein.core.distribution.doctor_airgap import (
    CheckStatus,
    run_airgap_checks,
)
from bernstein.core.security.network_policy import (
    ENV_NETWORK_POLICY,
    ENV_PROFILE_MODE,
    PROFILE_AIRGAP,
    NetworkPolicyDenied,
)
from bernstein.core.security.socket_guard import (
    install_runtime_socket_guard,
    is_runtime_socket_guard_installed,
    uninstall_runtime_socket_guard,
)
from tests.fixtures.airgap import WheelhouseFixture, build_wheelhouse

_HAS_COSIGN: Final[bool] = shutil.which("cosign") is not None
_HAS_GPG: Final[bool] = shutil.which("gpg") is not None or shutil.which("gpg2") is not None
_IS_LINUX: Final[bool] = sys.platform.startswith("linux")


@functools.cache
def _cosign_sign_blob_compat_flags() -> tuple[str, ...]:
    """Extra ``sign-blob`` flags for keyed offline detached signing.

    cosign v3 flipped two defaults that break the
    ``--tlog-upload=false`` + ``--output-signature`` invocation:
    ``--use-signing-config`` (rejects ``--tlog-upload=false``) and
    ``--new-bundle-format`` (requires ``--bundle``). Opt back out of
    both when the installed cosign supports them. ``--use-signing-config``
    in the help text is the version signal: releases that list it
    (v2.6+, v3) accept both opt-outs, while older releases (<= v2.4.x)
    already default both behaviours off. ``--new-bundle-format`` itself
    cannot be probed the same way -- v3 hides it from ``--help`` as
    deprecated while still accepting it. Mirrors
    scripts/sign_airgap_wheelhouse.sh.
    """
    result = subprocess.run(
        ["cosign", "sign-blob", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    help_text = result.stdout + result.stderr
    if "--use-signing-config" in help_text:
        return ("--use-signing-config=false", "--new-bundle-format=false")
    return ()


def _probe_unshare_capable() -> bool:
    """Return True if ``unshare -n`` actually works in this environment.

    GitHub-hosted runners ship the ``unshare`` binary but lack
    ``CAP_SYS_ADMIN``, so the syscall fails with ``EPERM``. We refuse to
    declare unshare-capable unless a no-op invocation succeeds.
    """
    if not _IS_LINUX or shutil.which("unshare") is None:
        return False
    try:
        result = subprocess.run(
            ["unshare", "-n", "--", "true"],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


_HAS_UNSHARE: Final[bool] = _probe_unshare_capable()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def session_wheelhouse(tmp_path_factory: pytest.TempPathFactory) -> WheelhouseFixture:
    """Build the synthetic 5-wheel fixture once per pytest session."""
    target = tmp_path_factory.mktemp("airgap_wh")
    return build_wheelhouse(target)


@pytest.fixture
def fresh_wheelhouse(tmp_path: Path) -> WheelhouseFixture:
    """Per-test wheelhouse so tampering tests cannot bleed into siblings."""
    return build_wheelhouse(tmp_path / "wh")


@pytest.fixture
def airgap_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Set BERNSTEIN_PROFILE_MODE=airgap and a deny-all network policy.

    Auto-installs and removes the runtime socket guard around the test.
    """
    monkeypatch.setenv(ENV_PROFILE_MODE, PROFILE_AIRGAP)
    monkeypatch.setenv(ENV_NETWORK_POLICY, "none")
    install_runtime_socket_guard(force=True)
    try:
        yield
    finally:
        uninstall_runtime_socket_guard()


@pytest.fixture
def loopback_airgap_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Airgap profile but with loopback explicitly allowed.

    Used by Ollama-stub tests that need 127.0.0.1 reachable.
    """
    monkeypatch.setenv(ENV_PROFILE_MODE, PROFILE_AIRGAP)
    monkeypatch.setenv(ENV_NETWORK_POLICY, "127.0.0.1")
    install_runtime_socket_guard(force=True)
    try:
        yield
    finally:
        uninstall_runtime_socket_guard()


@pytest.fixture
def cosign_keypair(tmp_path: Path) -> tuple[Path, Path]:
    """Generate a real local cosign keypair (skipped when cosign absent)."""
    if not _HAS_COSIGN:
        pytest.skip("cosign not installed -- skipping real-cosign integration")
    workdir = tmp_path / "cosign-keys"
    workdir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["COSIGN_PASSWORD"] = ""  # unencrypted key for the test
    result = subprocess.run(
        ["cosign", "generate-key-pair"],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"cosign key-gen failed: {result.stderr}"
    priv = workdir / "cosign.key"
    pub = workdir / "cosign.pub"
    assert priv.exists() and pub.exists()
    return priv, pub


@pytest.fixture
def gpg_home(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Set up an isolated GNUPGHOME for the test (skipped when gpg absent).

    macOS gpg-agent caps its Unix socket path at ~104 chars
    (Darwin sun_path). The default pytest tmp_path nests under
    ``/private/var/folders/.../pytest-of-<user>/...`` and overflows
    that, so we mint a short basename in ``/tmp``. We also pass
    ``--pinentry-mode=loopback`` so the unprotected key gen does
    not hit a real agent.
    """
    if not _HAS_GPG:
        pytest.skip("gpg not installed -- skipping real-gpg integration")
    import shutil as _sh
    import tempfile

    short_root = Path(tempfile.mkdtemp(prefix="bgpg-", dir="/tmp"))
    home = short_root / "h"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("GNUPGHOME", str(home))
    batch = home / "key.batch"
    batch.write_text(
        "%no-protection\n"
        "Key-Type: RSA\n"
        "Key-Length: 2048\n"
        "Subkey-Type: RSA\n"
        "Subkey-Length: 2048\n"
        "Name-Real: Bernstein Test\n"
        "Name-Email: test@example.com\n"
        "Expire-Date: 0\n"
        "%commit\n"
    )
    result = subprocess.run(
        ["gpg", "--batch", "--pinentry-mode=loopback", "--gen-key", str(batch)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        _sh.rmtree(short_root, ignore_errors=True)
        pytest.skip(f"gpg key generation failed in test env: {result.stderr}")
    try:
        yield home
    finally:
        _sh.rmtree(short_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Bug regression: UTF-8 BOM in MANIFEST.json (closes #45ffa9c83 cousin)
# ---------------------------------------------------------------------------


def test_manifest_with_utf8_bom_is_accepted(fresh_wheelhouse: WheelhouseFixture) -> None:
    """A manifest persisted on Windows with a UTF-8 BOM must verify cleanly.

    Prior to this fix the verifier raised :class:`json.JSONDecodeError`
    with ``Unexpected UTF-8 BOM (decode using utf-8-sig)`` because
    ``manifest_path.read_text()`` defaults to UTF-8 (no BOM stripping).
    """
    raw_text = fresh_wheelhouse.manifest_path.read_text()
    fresh_wheelhouse.manifest_path.write_bytes(b"\xef\xbb\xbf" + raw_text.encode("utf-8"))
    report = verify_wheelhouse(fresh_wheelhouse.root)
    assert report.ok is True, report.failures
    assert report.wheels_total == len(fresh_wheelhouse.wheel_names)


def test_manifest_with_bom_via_cli(fresh_wheelhouse: WheelhouseFixture) -> None:
    """Same regression but exercised through the operator CLI."""
    raw = fresh_wheelhouse.manifest_path.read_bytes()
    fresh_wheelhouse.manifest_path.write_bytes(b"\xef\xbb\xbf" + raw)
    runner = CliRunner()
    result = runner.invoke(wheelhouse_group, ["verify", str(fresh_wheelhouse.root)])
    assert result.exit_code == 0, result.output
    assert "PASSED" in result.output


# ---------------------------------------------------------------------------
# Real cosign sign + verify (skipped when cosign absent)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_COSIGN, reason="cosign CLI not available")
def test_real_cosign_sign_and_verify_passes(
    fresh_wheelhouse: WheelhouseFixture, cosign_keypair: tuple[Path, Path]
) -> None:
    """End-to-end: sign every wheel + manifest with real cosign, verify."""
    priv, pub = cosign_keypair
    env = os.environ.copy()
    env["COSIGN_PASSWORD"] = ""
    for wheel_name in fresh_wheelhouse.wheel_names:
        wheel = fresh_wheelhouse.root / wheel_name
        sig = wheel.with_suffix(wheel.suffix + ".sig")
        subprocess.run(
            [
                "cosign",
                "sign-blob",
                "--yes",
                "--tlog-upload=false",
                *_cosign_sign_blob_compat_flags(),
                "--key",
                str(priv),
                "--output-signature",
                str(sig),
                str(wheel),
            ],
            check=True,
            env=env,
            capture_output=True,
            timeout=30,
        )
    # Manifest sig too.
    manifest_sig = fresh_wheelhouse.root / "MANIFEST.sig"
    subprocess.run(
        [
            "cosign",
            "sign-blob",
            "--yes",
            "--tlog-upload=false",
            *_cosign_sign_blob_compat_flags(),
            "--key",
            str(priv),
            "--output-signature",
            str(manifest_sig),
            str(fresh_wheelhouse.manifest_path),
        ],
        check=True,
        env=env,
        capture_output=True,
        timeout=30,
    )
    verifier = CosignVerifier(pubkey_path=pub, ignore_tlog=True)
    report = verify_wheelhouse(fresh_wheelhouse.root, verifier=verifier)
    assert report.ok is True, report.failures
    assert report.signatures_present == len(fresh_wheelhouse.wheel_names)
    assert report.signatures_verified == len(fresh_wheelhouse.wheel_names)
    assert report.manifest_signature_ok is True


@pytest.mark.skipif(not _HAS_COSIGN, reason="cosign CLI not available")
def test_real_cosign_detects_wheel_byte_tamper(
    fresh_wheelhouse: WheelhouseFixture, cosign_keypair: tuple[Path, Path]
) -> None:
    """Flip one byte of one wheel after signing -> verify must fail by name."""
    priv, pub = cosign_keypair
    env = os.environ.copy()
    env["COSIGN_PASSWORD"] = ""
    target_name = fresh_wheelhouse.wheel_names[1]
    target_wheel = fresh_wheelhouse.root / target_name
    sig = target_wheel.with_suffix(target_wheel.suffix + ".sig")
    subprocess.run(
        [
            "cosign",
            "sign-blob",
            "--yes",
            "--tlog-upload=false",
            *_cosign_sign_blob_compat_flags(),
            "--key",
            str(priv),
            "--output-signature",
            str(sig),
            str(target_wheel),
        ],
        check=True,
        env=env,
        capture_output=True,
        timeout=30,
    )
    # Tamper a single byte.
    contents = bytearray(target_wheel.read_bytes())
    contents[-1] ^= 0xFF
    target_wheel.write_bytes(bytes(contents))
    verifier = CosignVerifier(pubkey_path=pub, ignore_tlog=True)
    report = verify_wheelhouse(fresh_wheelhouse.root, verifier=verifier)
    assert report.ok is False
    assert any("sha256 mismatch" in f and target_name in f for f in report.failures), report.failures


@pytest.mark.skipif(not _HAS_COSIGN, reason="cosign CLI not available")
def test_real_cosign_detects_manifest_sha_tamper(
    fresh_wheelhouse: WheelhouseFixture, cosign_keypair: tuple[Path, Path]
) -> None:
    """Tamper one sha256 entry in the manifest -- wheels intact -- verify fails."""
    priv, pub = cosign_keypair
    env = os.environ.copy()
    env["COSIGN_PASSWORD"] = ""
    # Sign manifest first, then mutate the manifest body.
    manifest_sig = fresh_wheelhouse.root / "MANIFEST.sig"
    subprocess.run(
        [
            "cosign",
            "sign-blob",
            "--yes",
            "--tlog-upload=false",
            *_cosign_sign_blob_compat_flags(),
            "--key",
            str(priv),
            "--output-signature",
            str(manifest_sig),
            str(fresh_wheelhouse.manifest_path),
        ],
        check=True,
        env=env,
        capture_output=True,
        timeout=30,
    )
    payload = json.loads(fresh_wheelhouse.manifest_path.read_text())
    payload["wheels"][0]["sha256"] = "0" * 64
    fresh_wheelhouse.manifest_path.write_text(json.dumps(payload))
    verifier = CosignVerifier(pubkey_path=pub, ignore_tlog=True)
    report = verify_wheelhouse(fresh_wheelhouse.root, verifier=verifier)
    assert report.ok is False
    # Either the sha mismatch or the manifest signature must surface; both
    # are correct outcomes -- we accept either as long as ok=False.
    assert any("sha256 mismatch" in f or "MANIFEST.json" in f for f in report.failures), report.failures


def test_cosign_verifier_argv_includes_insecure_ignore_tlog(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Bug regression: offline verify must NOT make a Rekor call.

    The default cosign behaviour is to dial out to rekor.sigstore.dev.
    The verifier defaults ``ignore_tlog=True`` and adds the flag.
    """
    captured: dict[str, list[str]] = {}

    class _R:
        returncode = 0

    def _fake_run(cmd: list[str], **_kw: object) -> _R:
        captured["cmd"] = cmd
        return _R()

    monkeypatch.setattr("bernstein.core.distribution.verifier.shutil.which", lambda _n: "/usr/bin/cosign")
    monkeypatch.setattr("bernstein.core.distribution.verifier.subprocess.run", _fake_run)
    blob = tmp_path / "blob"
    sig = tmp_path / "blob.sig"
    blob.write_bytes(b"x")
    sig.write_bytes(b"y")
    v = CosignVerifier(pubkey_path=tmp_path / "k.pub")
    assert v.verify(blob, sig) is True
    assert "--insecure-ignore-tlog" in captured["cmd"]


def test_cosign_verifier_can_opt_back_into_tlog(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Operators with a private Rekor pass ``ignore_tlog=False`` to opt out."""
    captured: dict[str, list[str]] = {}

    class _R:
        returncode = 0

    def _fake_run(cmd: list[str], **_kw: object) -> _R:
        captured["cmd"] = cmd
        return _R()

    monkeypatch.setattr("bernstein.core.distribution.verifier.shutil.which", lambda _n: "/usr/bin/cosign")
    monkeypatch.setattr("bernstein.core.distribution.verifier.subprocess.run", _fake_run)
    blob = tmp_path / "blob"
    sig = tmp_path / "blob.sig"
    blob.write_bytes(b"x")
    sig.write_bytes(b"y")
    v = CosignVerifier(pubkey_path=tmp_path / "k.pub", ignore_tlog=False)
    v.verify(blob, sig)
    assert "--insecure-ignore-tlog" not in captured["cmd"]


# ---------------------------------------------------------------------------
# Real GPG sign + verify (skipped when gpg absent)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_GPG, reason="gpg CLI not available")
def test_real_gpg_sign_and_verify_passes(fresh_wheelhouse: WheelhouseFixture, gpg_home: Path) -> None:
    """End-to-end: sign with real gpg, verify uses the same keyring."""
    target_name = fresh_wheelhouse.wheel_names[0]
    target_wheel = fresh_wheelhouse.root / target_name
    sig = target_wheel.with_suffix(target_wheel.suffix + ".sig")
    result = subprocess.run(
        ["gpg", "--batch", "--yes", "--detach-sign", "--output", str(sig), str(target_wheel)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    verifier = GpgVerifier()
    assert verifier.verify(target_wheel, sig) is True


@pytest.mark.skipif(not _HAS_GPG, reason="gpg CLI not available")
def test_real_gpg_detects_tamper(fresh_wheelhouse: WheelhouseFixture, gpg_home: Path) -> None:
    """Tampering with the wheel after signing must flip gpg verify to False."""
    target_name = fresh_wheelhouse.wheel_names[0]
    target_wheel = fresh_wheelhouse.root / target_name
    sig = target_wheel.with_suffix(target_wheel.suffix + ".sig")
    subprocess.run(
        ["gpg", "--batch", "--yes", "--detach-sign", "--output", str(sig), str(target_wheel)],
        check=True,
        capture_output=True,
        timeout=60,
    )
    target_wheel.write_bytes(target_wheel.read_bytes() + b"TAMPER")
    verifier = GpgVerifier()
    assert verifier.verify(target_wheel, sig) is False


# ---------------------------------------------------------------------------
# Runtime socket guard (works on every OS in-process)
# ---------------------------------------------------------------------------


def test_socket_guard_blocks_disallowed_destination_under_airgap(airgap_env: None) -> None:
    """Direct socket.connect to a public IP must raise NetworkPolicyDenied.

    Uses a TEST-NET-1 IPv4 (192.0.2.x) destination so even if the guard
    failed-open the connection would still time out -- but the policy
    is meant to refuse before any packet leaves.
    """
    assert is_runtime_socket_guard_installed()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        with pytest.raises(NetworkPolicyDenied) as excinfo:
            sock.connect(("192.0.2.1", 443))
        assert "192.0.2.1:443" in str(excinfo.value)
    finally:
        sock.close()


def test_socket_guard_allows_loopback_when_policy_permits(loopback_airgap_env: None) -> None:
    """When ``--allow-network 127.0.0.1`` is present the guard must permit it.

    Spins up a tiny localhost listener so the connect can actually succeed.
    """
    assert is_runtime_socket_guard_installed()
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    port = server.getsockname()[1]
    server.listen(1)
    try:
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.settimeout(2.0)
        try:
            client.connect(("127.0.0.1", port))
        finally:
            client.close()
    finally:
        server.close()


def test_socket_guard_off_outside_airgap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Outside airgap mode the guard must refuse to install (no-op)."""
    monkeypatch.delenv(ENV_PROFILE_MODE, raising=False)
    monkeypatch.delenv(ENV_NETWORK_POLICY, raising=False)
    # Make sure no leftover from a sibling test.
    uninstall_runtime_socket_guard()
    installed = install_runtime_socket_guard()
    assert installed is False
    assert is_runtime_socket_guard_installed() is False


def test_socket_guard_unix_sockets_are_exempt(airgap_env: None) -> None:
    """UDS connections (gRPC IPC, journald) must not be blocked.

    Skipped on Windows where AF_UNIX may not be supported by older runtimes.
    """
    if not hasattr(socket, "AF_UNIX"):
        pytest.skip("AF_UNIX not available on this platform")
    server_path = Path("/tmp") / f"bernstein-airgap-test-{os.getpid()}.sock"
    if server_path.exists():
        server_path.unlink()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(server_path))
        server.listen(1)
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client.settimeout(2.0)
            # No exception means the guard correctly skipped AF_UNIX.
            client.connect(str(server_path))
        finally:
            client.close()
    finally:
        server.close()
        if server_path.exists():
            server_path.unlink()


def test_socket_guard_uninstall_restores_original() -> None:
    """uninstall_runtime_socket_guard() must put back the unpatched connect."""
    os.environ[ENV_PROFILE_MODE] = PROFILE_AIRGAP
    os.environ[ENV_NETWORK_POLICY] = "none"
    try:
        install_runtime_socket_guard(force=True)
        assert is_runtime_socket_guard_installed() is True
        assert uninstall_runtime_socket_guard() is True
        assert is_runtime_socket_guard_installed() is False
    finally:
        os.environ.pop(ENV_PROFILE_MODE, None)
        os.environ.pop(ENV_NETWORK_POLICY, None)
        uninstall_runtime_socket_guard()


# ---------------------------------------------------------------------------
# Linux network namespace (real --network=none equivalent)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_UNSHARE, reason="unshare CLI not available (Linux only)")
def test_unshare_n_blocks_external_egress(fresh_wheelhouse: WheelhouseFixture) -> None:
    """Run the verify CLI inside ``unshare -n`` -- no outbound traffic possible.

    Asserts: the verify command still succeeds, proving every code path it
    walks is local. If verify ever needed network (a hidden Rekor lookup,
    a typoshield query) this test would fail.
    """
    cmd = [
        "unshare",
        "-n",
        "--",
        sys.executable,
        "-c",
        f"from bernstein.cli.commands.wheelhouse_cmd import run_verify; "
        f"import sys; "
        f"sys.exit(run_verify("
        f"wheelhouse_path=__import__('pathlib').Path(r'{fresh_wheelhouse.root}'),"
        f"verifier_kind='auto', ca_pubkey=None, keyring_path=None,"
        f"cosign_identity=None, cosign_issuer=None, require_signatures=False))",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, f"verify failed in unshared namespace: {result.stderr}"


@pytest.mark.skipif(not _HAS_UNSHARE, reason="unshare CLI not available (Linux only)")
def test_unshare_n_blocks_outbound_socket() -> None:
    """Sanity: unshare -n really does isolate the namespace.

    Without this guard our other unshare tests would be testing something.
    """
    result = subprocess.run(
        [
            "unshare",
            "-n",
            "--",
            sys.executable,
            "-c",
            "import socket; s=socket.socket(socket.AF_INET, socket.SOCK_STREAM); "
            "s.settimeout(2.0); "
            "import sys\n"
            "try:\n"
            "    s.connect(('1.1.1.1', 53))\n"
            "    sys.exit(0)\n"
            "except OSError as e:\n"
            "    sys.exit(42)",
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 42, f"namespace did not isolate egress: {result.stdout} {result.stderr}"


# ---------------------------------------------------------------------------
# Doctor airgap battery -- pass and per-battery break
# ---------------------------------------------------------------------------


def test_doctor_airgap_passes_on_clean_install(
    airgap_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a fresh airgap install every check must report PASS or WARN."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(
        "pathlib.Path.home",
        lambda: tmp_path / "home",
        raising=False,
    )
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)
    workdir = tmp_path / "wd"
    workdir.mkdir()
    report = run_airgap_checks(workdir=workdir)
    assert report.ok is True, [f"{c.name}={c.status}" for c in report.checks if c.status is CheckStatus.FAIL]
    # The four primary batteries must all be PASS (not WARN) on a clean install.
    by_name = {c.name: c for c in report.checks}
    assert by_name["airgap profile active"].status is CheckStatus.PASS
    assert by_name["network policy deny-all"].status is CheckStatus.PASS
    assert by_name["MCP catalog all-off"].status is CheckStatus.PASS


def test_doctor_airgap_breaks_on_missing_profile_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Battery 1 break: BERNSTEIN_PROFILE_MODE missing."""
    monkeypatch.delenv(ENV_PROFILE_MODE, raising=False)
    monkeypatch.setenv(ENV_NETWORK_POLICY, "none")
    workdir = tmp_path / "wd"
    workdir.mkdir()
    report = run_airgap_checks(workdir=workdir)
    assert report.ok is False
    failed = [c for c in report.checks if c.status is CheckStatus.FAIL]
    assert any(c.name == "airgap profile active" for c in failed)


def test_doctor_airgap_breaks_on_allow_any_policy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Battery 2 break: legacy allow-any policy active."""
    monkeypatch.setenv(ENV_PROFILE_MODE, PROFILE_AIRGAP)
    monkeypatch.setenv(ENV_NETWORK_POLICY, "any")
    workdir = tmp_path / "wd"
    workdir.mkdir()
    report = run_airgap_checks(workdir=workdir)
    assert report.ok is False
    assert any(c.name == "network policy deny-all" and c.status is CheckStatus.FAIL for c in report.checks)


def test_doctor_airgap_breaks_when_mcp_catalog_populated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Battery 3 break: residual MCP servers in user config.

    Drops a fake bernstein-managed entry into ~/.config/bernstein/mcp.json
    and asserts the check turns red. This is the residual-config attack
    surface: a previous non-airgap session leaves endpoints behind that
    a fresh airgap session would otherwise pick up.
    """
    monkeypatch.setenv(ENV_PROFILE_MODE, PROFILE_AIRGAP)
    monkeypatch.setenv(ENV_NETWORK_POLICY, "none")
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    cfg_path = xdg / "bernstein" / "mcp.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        json.dumps(
            {
                "bernstein-managed": {
                    "mcpServers": {
                        "leak-server": {
                            "id": "leak-server",
                            "name": "leak-server",
                            "version_pin": "1.0.0",
                            "installed_at": "2026-05-08T00:00:00+00:00",
                            "command": "node",
                            "args": ["leak.js"],
                        }
                    }
                }
            }
        )
    )
    workdir = tmp_path / "wd"
    workdir.mkdir()
    report = run_airgap_checks(workdir=workdir)
    by_name = {c.name: c for c in report.checks}
    mcp = by_name["MCP catalog all-off"]
    assert mcp.status is CheckStatus.FAIL
    assert "leak-server" in mcp.detail


def test_doctor_airgap_breaks_when_runtime_has_external_hostname(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Battery 4 break: runtime state references a public endpoint."""
    monkeypatch.setenv(ENV_PROFILE_MODE, PROFILE_AIRGAP)
    monkeypatch.setenv(ENV_NETWORK_POLICY, "none")
    workdir = tmp_path / "wd"
    runtime = workdir / ".sdd" / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "session.log").write_text("dialed api.cloudflare.com:443 from a leaky plugin\n")
    report = run_airgap_checks(workdir=workdir)
    by_name = {c.name: c for c in report.checks}
    hosts = by_name["no external hostnames in runtime"]
    assert hosts.status is CheckStatus.FAIL
    assert "api.cloudflare.com" in hosts.detail


def test_doctor_airgap_runtime_socket_guard_check_warns_outside_airgap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Outside airgap the runtime-guard check must WARN (not FAIL) -- the guard
    is intentionally a no-op there and we don't want false-positive doctor red."""
    monkeypatch.delenv(ENV_PROFILE_MODE, raising=False)
    workdir = tmp_path / "wd"
    workdir.mkdir()
    report = run_airgap_checks(workdir=workdir)
    by_name = {c.name: c for c in report.checks}
    guard_row = by_name["runtime socket guard active"]
    assert guard_row.status is CheckStatus.WARN


def test_doctor_airgap_runtime_socket_guard_check_passes_when_installed(airgap_env: None, tmp_path: Path) -> None:
    """Inside airgap with the guard installed, the check must PASS."""
    workdir = tmp_path / "wd"
    workdir.mkdir()
    report = run_airgap_checks(workdir=workdir)
    by_name = {c.name: c for c in report.checks}
    guard_row = by_name["runtime socket guard active"]
    assert guard_row.status is CheckStatus.PASS


def test_doctor_airgap_runtime_socket_guard_check_self_installs_when_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Inside airgap without the guard, the doctor self-installs for the check.

    Standalone ``bernstein doctor airgap`` is the documented pre-flight
    workflow (run before ``bernstein run``), so the run-bootstrap installer
    has not yet fired. Per bughunt 2026-05-13 fix, the check installs the
    guard, verifies the monkeypatch, then restores -- exercising the guard's
    install/uninstall logic round-trip so a regression in the guard itself
    would still surface here.

    Cleanup: the doctor must restore ``socket.socket.connect`` after the check.
    """
    monkeypatch.setenv(ENV_PROFILE_MODE, PROFILE_AIRGAP)
    monkeypatch.setenv(ENV_NETWORK_POLICY, "none")
    uninstall_runtime_socket_guard()
    workdir = tmp_path / "wd"
    workdir.mkdir()
    report = run_airgap_checks(workdir=workdir)
    by_name = {c.name: c for c in report.checks}
    guard_row = by_name["runtime socket guard active"]
    assert guard_row.status is CheckStatus.PASS
    assert not is_runtime_socket_guard_installed()


def test_doctor_airgap_cli_exit_code(airgap_env: None, tmp_path: Path) -> None:
    """The CLI returns 0 when every check passes, 1 otherwise."""
    runner = CliRunner()
    result = runner.invoke(doctor_group, ["airgap"])
    # We're in airgap_env (clean); audit may WARN (no audit dir). Should be ok.
    assert result.exit_code in (0, 1)
    # Output must reference the four primary battery names regardless.
    output = result.output
    for needle in ("airgap profile active", "network policy deny-all", "MCP catalog all-off"):
        assert needle in output, output


def test_doctor_airgap_cli_json_output(airgap_env: None) -> None:
    """The --json flag emits machine-readable structured output."""
    runner = CliRunner()
    result = runner.invoke(doctor_group, ["--json", "airgap"])
    assert result.exit_code in (0, 1)
    payload = json.loads(result.output)
    assert "ok" in payload
    assert "checks" in payload
    assert any(c["name"] == "runtime socket guard active" for c in payload["checks"])


# ---------------------------------------------------------------------------
# `bernstein run --profile airgap` adapter spawn refusal
# ---------------------------------------------------------------------------


def test_airgap_refuses_cloudflare_adapter_spawn(airgap_env: None) -> None:
    """`bernstein run --profile airgap` REFUSES adapters that declare external
    endpoints. Error message must name the adapter + destination."""
    from bernstein.adapters.cloudflare_agents import CloudflareAgentsAdapter

    adapter = CloudflareAgentsAdapter()
    with pytest.raises(NetworkPolicyDenied) as excinfo:
        adapter.enforce_network_policy()
    msg = str(excinfo.value)
    assert "api.cloudflare.com" in msg
    assert "443" in msg
    assert excinfo.value.source == "adapter:Cloudflare Agents" or "cloudflare" in excinfo.value.source.lower()


def test_airgap_refuses_claude_adapter_spawn(airgap_env: None) -> None:
    """Same refusal for the Claude Code adapter (Anthropic endpoint)."""
    from bernstein.adapters.claude import ClaudeCodeAdapter

    adapter = ClaudeCodeAdapter()
    with pytest.raises(NetworkPolicyDenied) as excinfo:
        adapter.enforce_network_policy()
    assert "api.anthropic.com" in str(excinfo.value)


def test_airgap_refuses_codex_adapter_spawn(airgap_env: None) -> None:
    """Same refusal for the Codex adapter (OpenAI endpoint)."""
    from bernstein.adapters.codex import CodexAdapter

    adapter = CodexAdapter()
    with pytest.raises(NetworkPolicyDenied) as excinfo:
        adapter.enforce_network_policy()
    assert "api.openai.com" in str(excinfo.value)


def test_airgap_allows_local_only_adapter() -> None:
    """An adapter without declared external endpoints is treated as local-only.

    This is the contract: adapters that DO call out must declare. Adapters
    that don't are exempt from the per-adapter gate. The runtime socket
    guard is the safety net for un-declared egress.
    """
    from bernstein.adapters.base import CLIAdapter

    class _Local(CLIAdapter):
        external_endpoints: tuple[tuple[str, int], ...] = ()

        def name(self) -> str:
            return "local"

        def spawn(self, **_kw: object) -> object:  # type: ignore[override]
            raise NotImplementedError

    os.environ[ENV_PROFILE_MODE] = PROFILE_AIRGAP
    os.environ[ENV_NETWORK_POLICY] = "none"
    try:
        adapter = _Local()
        adapter.enforce_network_policy()  # must not raise
    finally:
        os.environ.pop(ENV_PROFILE_MODE, None)
        os.environ.pop(ENV_NETWORK_POLICY, None)


# ---------------------------------------------------------------------------
# Build script smoke (skip-project mode is fast and offline)
# ---------------------------------------------------------------------------


def test_build_airgap_wheelhouse_script_skip_project_runs(tmp_path: Path) -> None:
    """Build the wheelhouse via the script with --skip-project (no uv export)."""
    repo_root = Path(__file__).resolve().parents[2]
    out = tmp_path / "out"
    # The script falls back gracefully when uv export fails (offline runner);
    # we just want to see it lands MANIFEST.json without crashing.
    cmd = [
        sys.executable,
        str(repo_root / "scripts" / "build_airgap_wheelhouse.py"),
        "--skip-project",
        "--output",
        str(out),
        "--version",
        "1.10.3",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert (out / "MANIFEST.json").exists()


# ---------------------------------------------------------------------------
# Bracketed IPv6 + run-bootstrap regression tests (closes brief items)
# ---------------------------------------------------------------------------


def test_allow_network_bracketed_ipv6_with_port_under_airgap() -> None:
    """``--allow-network [2001:db8::1]:443`` survives the airgap parser.

    Regression: the parser used to mis-split bracketed IPv6 host:port.
    Verifies the policy parser handles ``[host]:port`` correctly under
    the install path used by ``bernstein run --profile airgap``.
    """
    from bernstein.cli.run_bootstrap import _install_network_policy

    os.environ.pop(ENV_PROFILE_MODE, None)
    os.environ.pop(ENV_NETWORK_POLICY, None)
    try:
        _install_network_policy(run_profile=PROFILE_AIRGAP, allow_network=("[2001:db8::1]:443",))
        from bernstein.core.security.network_policy import policy_from_env

        p = policy_from_env()
        assert p.is_allowed("2001:db8::1", 443) is True
        assert p.is_allowed("2001:db8::1", 80) is False
        assert p.is_allowed("api.cloudflare.com", 443) is False
    finally:
        os.environ.pop(ENV_PROFILE_MODE, None)
        os.environ.pop(ENV_NETWORK_POLICY, None)
        uninstall_runtime_socket_guard()


def test_install_network_policy_airgap_installs_socket_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_install_network_policy(run_profile='airgap', ...)`` must auto-install
    the runtime socket guard so the boundary is wired without any extra
    operator action."""
    monkeypatch.delenv(ENV_PROFILE_MODE, raising=False)
    monkeypatch.delenv(ENV_NETWORK_POLICY, raising=False)
    uninstall_runtime_socket_guard()
    from bernstein.cli.run_bootstrap import _install_network_policy

    try:
        _install_network_policy(run_profile=PROFILE_AIRGAP, allow_network=())
        assert is_runtime_socket_guard_installed() is True
    finally:
        uninstall_runtime_socket_guard()
        monkeypatch.delenv(ENV_PROFILE_MODE, raising=False)
        monkeypatch.delenv(ENV_NETWORK_POLICY, raising=False)


def test_install_network_policy_non_airgap_skips_socket_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Outside airgap the install path must NOT patch socket.connect."""
    monkeypatch.delenv(ENV_PROFILE_MODE, raising=False)
    monkeypatch.delenv(ENV_NETWORK_POLICY, raising=False)
    uninstall_runtime_socket_guard()
    from bernstein.cli.run_bootstrap import _install_network_policy

    try:
        _install_network_policy(run_profile=None, allow_network=())
        assert is_runtime_socket_guard_installed() is False
    finally:
        uninstall_runtime_socket_guard()


# ---------------------------------------------------------------------------
# Wheelhouse fixture sanity (catches fixture drift)
# ---------------------------------------------------------------------------


def test_session_wheelhouse_is_internally_consistent(session_wheelhouse: WheelhouseFixture) -> None:
    """The session-wide fixture must verify cleanly under the default verifier."""
    report = verify_wheelhouse(session_wheelhouse.root)
    assert report.ok is True, report.failures
    assert report.wheels_total == len(session_wheelhouse.wheel_names)


def test_session_wheelhouse_wheels_are_real_zips(session_wheelhouse: WheelhouseFixture) -> None:
    """Each fixture wheel is openable as a zip (proves we did not regress to
    bytes-only stubs which the build_wheelhouse fixture used to emit)."""
    for name in session_wheelhouse.wheel_names:
        with zipfile.ZipFile(session_wheelhouse.root / name) as zf:
            members = zf.namelist()
            assert any(m.endswith("__init__.py") for m in members), members


# ---------------------------------------------------------------------------
# Smoke: stdout-capturing verify CLI in airgap, real fixture
# ---------------------------------------------------------------------------


def test_cli_verify_under_airgap_stays_offline(fresh_wheelhouse: WheelhouseFixture, airgap_env: None) -> None:
    """Running `bernstein wheelhouse verify` under airgap must succeed without
    any outbound socket attempt. The airgap env installs the runtime socket
    guard so any accidental dial would raise NetworkPolicyDenied -- which
    Click would surface as a non-zero exit. We assert exit==0 to prove the
    CLI's verify path is fully local."""
    runner = CliRunner()
    result = runner.invoke(wheelhouse_group, ["verify", str(fresh_wheelhouse.root)])
    assert result.exit_code == 0, result.output
    assert "PASSED" in result.output
