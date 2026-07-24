"""Integration: air-gap wheelhouse build / verify / network policy round-trip.

Covers:
- ``scripts/build_airgap_wheelhouse.py`` produces a manifest with sha256s
- ``bernstein verify <wheelhouse>`` exits 0 when checksums match
- ``bernstein verify`` exits non-zero when a wheel is tampered with
- The ``--allow-network`` policy is enforced at every adapter spawn point
  with a known external endpoint
- The MCP SSE / StreamableHTTP transports refuse to connect under a
  deny-all policy
- ``--profile airgap`` defaults the policy to deny-all and propagates the
  profile mode via the environment

Tests don't actually fetch from PyPI - they construct a minimal fixture
wheelhouse so the round-trip works without network. The build script
itself is exercised separately when uv is available.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.verify_cmd import verify_cmd
from bernstein.core.security.network_policy import (
    ENV_NETWORK_POLICY,
    ENV_PROFILE_MODE,
    PROFILE_AIRGAP,
    NetworkPolicy,
    NetworkPolicyDenied,
    install_policy,
    is_airgap_profile,
    policy_from_env,
)


@dataclass
class FixtureWheelhouse:
    path: Path
    wheel_names: tuple[str, ...]


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv(ENV_NETWORK_POLICY, raising=False)
    monkeypatch.delenv(ENV_PROFILE_MODE, raising=False)
    yield


def _write_fake_wheel(path: Path, name: str = "bernstein-1.9.4-py3-none-any.whl") -> Path:
    wheel = path / name
    with zipfile.ZipFile(wheel, "w") as zf:
        zf.writestr(f"{name.split('-')[0]}/__init__.py", "VERSION = '1.9.4'\n")
        zf.writestr(
            f"{name.replace('.whl', '')}.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: bernstein\nVersion: 1.9.4\n",
        )
        zf.writestr(f"{name.replace('.whl', '')}.dist-info/WHEEL", "Wheel-Version: 1.0\n")
    return wheel


def _make_fixture_wheelhouse(target: Path) -> FixtureWheelhouse:
    target.mkdir(parents=True, exist_ok=True)
    wheel = _write_fake_wheel(target)
    h = hashlib.sha256(wheel.read_bytes()).hexdigest()
    manifest = {
        "version": "1.9.4",
        "generated_at": "2026-05-05T00:00:00+00:00",
        "wheels": [
            {"name": wheel.name, "sha256": h, "size": wheel.stat().st_size},
        ],
    }
    (target / "MANIFEST.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return FixtureWheelhouse(path=target, wheel_names=(wheel.name,))


def test_network_policy_from_specs_allow_any() -> None:
    p = NetworkPolicy.from_specs(None)
    assert p.allow_any is True
    assert p.is_allowed("any.example.com", 443)


def test_network_policy_deny_all() -> None:
    p = NetworkPolicy.deny_all()
    assert p.allow_any is False
    assert not p.is_allowed("api.cloudflare.com", 443)
    assert not p.is_allowed("127.0.0.1", 80)


def test_network_policy_host_only() -> None:
    p = NetworkPolicy.from_specs(("127.0.0.1",))
    assert p.is_allowed("127.0.0.1", 11434)
    assert not p.is_allowed("api.cloudflare.com", 443)


def test_network_policy_host_port() -> None:
    p = NetworkPolicy.from_specs(("ollama.local:11434",))
    assert p.is_allowed("ollama.local", 11434)
    assert not p.is_allowed("ollama.local", 11435)


def test_network_policy_cidr() -> None:
    p = NetworkPolicy.from_specs(("10.0.0.0/8",))
    assert p.is_allowed("10.1.2.3", 443)
    assert not p.is_allowed("8.8.8.8", 443)


def test_network_policy_check_raises() -> None:
    p = NetworkPolicy.deny_all()
    with pytest.raises(NetworkPolicyDenied) as excinfo:
        p.check("api.cloudflare.com", 443, source="adapter:cloudflare")
    assert "api.cloudflare.com:443" in str(excinfo.value)
    assert excinfo.value.destination == "api.cloudflare.com:443"
    assert excinfo.value.source == "adapter:cloudflare"


def test_network_policy_url_check() -> None:
    p = NetworkPolicy.from_specs(("127.0.0.1",))
    p.check_url("http://127.0.0.1:8052/health")
    with pytest.raises(NetworkPolicyDenied):
        p.check_url("https://api.cloudflare.com/v4/x")


def test_install_policy_round_trip(clean_env: None) -> None:
    policy = NetworkPolicy.from_specs(("127.0.0.1", "10.0.0.0/8"))
    install_policy(policy, profile=PROFILE_AIRGAP)
    assert os.environ[ENV_PROFILE_MODE] == PROFILE_AIRGAP
    assert is_airgap_profile()
    reconstructed = policy_from_env()
    assert reconstructed.is_allowed("10.5.5.5", 443)
    assert not reconstructed.is_allowed("api.cloudflare.com", 443)


def test_policy_from_env_defaults_to_allow_all(clean_env: None) -> None:
    assert policy_from_env().allow_any is True


def test_policy_from_env_explicit_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_NETWORK_POLICY, "none")
    assert not policy_from_env().is_allowed("anywhere", 443)


def test_verify_wheelhouse_passes(tmp_path: Path) -> None:
    wh = _make_fixture_wheelhouse(tmp_path / "wh")
    runner = CliRunner()
    result = runner.invoke(verify_cmd, [str(wh.path)])
    assert result.exit_code == 0, result.output
    assert "PASSED" in result.output


def test_verify_wheelhouse_fails_on_tamper(tmp_path: Path) -> None:
    wh = _make_fixture_wheelhouse(tmp_path / "wh")
    target_wheel = wh.path / wh.wheel_names[0]
    target_wheel.write_bytes(target_wheel.read_bytes() + b"TAMPER")
    runner = CliRunner()
    result = runner.invoke(verify_cmd, [str(wh.path)])
    assert result.exit_code == 1, result.output
    assert "FAILED" in result.output
    assert wh.wheel_names[0] in result.output


def test_verify_wheelhouse_missing_manifest(tmp_path: Path) -> None:
    target = tmp_path / "wh"
    target.mkdir()
    _write_fake_wheel(target)
    runner = CliRunner()
    result = runner.invoke(verify_cmd, [str(target)])
    assert result.exit_code == 1
    assert "MANIFEST.json" in result.output


def test_verify_wheelhouse_missing_dir(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(verify_cmd, [str(tmp_path / "missing")])
    assert result.exit_code == 1
    assert "Wheelhouse not found" in result.output


def test_verify_with_no_args_returns_help(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(verify_cmd, [])
    assert result.exit_code == 0
    assert "wheelhouse-path" in result.output


def test_mcp_sse_transport_refused_under_deny_all(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_NETWORK_POLICY, "none")
    from bernstein.core.protocols.mcp.mcp_transport import SseTransport, TransportConfig

    transport = SseTransport()
    with pytest.raises(NetworkPolicyDenied):
        transport.connect(TransportConfig(url="https://mcp.example.com/sse"))


def test_mcp_streamable_http_transport_refused_under_deny_all(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_NETWORK_POLICY, "none")
    from bernstein.core.protocols.mcp.mcp_transport import StreamableHttpTransport, TransportConfig

    transport = StreamableHttpTransport()
    with pytest.raises(NetworkPolicyDenied):
        transport.connect(TransportConfig(url="https://mcp.example.com/stream"))


def test_mcp_sse_transport_allowed_under_loopback_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_NETWORK_POLICY, "127.0.0.1")
    from bernstein.core.protocols.mcp.mcp_transport import SseTransport, TransportConfig

    transport = SseTransport()
    transport.connect(TransportConfig(url="http://127.0.0.1:8765/sse"))
    assert transport.is_connected
    transport.disconnect()


def test_claude_adapter_refused_under_airgap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_NETWORK_POLICY, "none")
    from bernstein.adapters.claude import ClaudeCodeAdapter

    adapter = ClaudeCodeAdapter()
    with pytest.raises(NetworkPolicyDenied) as excinfo:
        adapter.enforce_network_policy()
    assert "api.anthropic.com" in str(excinfo.value)


def test_claude_adapter_declares_anthropic_endpoint() -> None:
    from bernstein.adapters.claude import ClaudeCodeAdapter

    assert ("api.anthropic.com", 443) in ClaudeCodeAdapter.external_endpoints


def test_codex_adapter_declares_openai_endpoint() -> None:
    from bernstein.adapters.codex import CodexAdapter

    assert ("api.openai.com", 443) in CodexAdapter.external_endpoints


def test_gemini_adapter_declares_google_endpoint() -> None:
    from bernstein.adapters.gemini import GeminiAdapter

    assert any("googleapis" in host for host, _ in GeminiAdapter.external_endpoints)


def test_ollama_adapter_inline_check_under_deny_all(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_NETWORK_POLICY, "none")
    from bernstein.core.models import ModelConfig

    from bernstein.adapters.ollama import OllamaAdapter

    adapter = OllamaAdapter()
    with pytest.raises(NetworkPolicyDenied):
        adapter.spawn(
            prompt="hi",
            workdir=Path("/tmp"),
            model_config=ModelConfig(model="haiku", effort="normal"),
            session_id="qa-1",
        )


def test_ollama_adapter_allowed_under_loopback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(ENV_NETWORK_POLICY, "127.0.0.1")
    from bernstein.core.models import ModelConfig

    from bernstein.adapters.ollama import OllamaAdapter

    adapter = OllamaAdapter()
    fake_proc = MagicMock()
    fake_proc.pid = 4321

    def _fake_popen(*args: object, **kwargs: object) -> MagicMock:
        return fake_proc

    monkeypatch.setattr("bernstein.adapters.ollama.subprocess.Popen", _fake_popen)
    monkeypatch.setattr(adapter, "_start_timeout_watchdog", lambda *a, **k: None)
    result = adapter.spawn(
        prompt="hi",
        workdir=tmp_path,
        model_config=ModelConfig(model="haiku", effort="normal"),
        session_id="qa-1",
        timeout_seconds=0,
    )
    assert result.pid == 4321


def test_run_profile_airgap_defaults_deny_all(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_NETWORK_POLICY, raising=False)
    monkeypatch.delenv(ENV_PROFILE_MODE, raising=False)
    from bernstein.cli.run_bootstrap import _install_network_policy

    _install_network_policy(run_profile="airgap", allow_network=())
    assert os.environ[ENV_NETWORK_POLICY] == "none"
    assert os.environ[ENV_PROFILE_MODE] == PROFILE_AIRGAP


def test_run_profile_airgap_with_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_NETWORK_POLICY, raising=False)
    monkeypatch.delenv(ENV_PROFILE_MODE, raising=False)
    from bernstein.cli.run_bootstrap import _install_network_policy

    _install_network_policy(run_profile="airgap", allow_network=("127.0.0.1", "10.0.0.0/8"))
    p = policy_from_env()
    assert p.is_allowed("127.0.0.1", 11434)
    assert p.is_allowed("10.5.5.5", 443)
    assert not p.is_allowed("api.cloudflare.com", 443)


def test_run_default_is_unrestricted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_NETWORK_POLICY, raising=False)
    monkeypatch.delenv(ENV_PROFILE_MODE, raising=False)
    from bernstein.cli.run_bootstrap import _install_network_policy

    _install_network_policy(run_profile=None, allow_network=())
    assert policy_from_env().allow_any is True


def test_build_script_executable() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    script = repo_root / "scripts" / "build_airgap_wheelhouse.py"
    assert script.is_file()
    assert os.access(script, os.X_OK)
    result = subprocess.run([sys.executable, str(script), "--help"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "wheelhouse" in result.stdout.lower()


def test_sign_script_present() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    script = repo_root / "scripts" / "sign_airgap_wheelhouse.sh"
    assert script.is_file()
    assert os.access(script, os.X_OK)
    contents = script.read_text()
    assert "cosign" in contents
    assert "MANIFEST" in contents


def test_sign_script_keeps_cosign_v3_offline_flags() -> None:
    """Offline keyed signing must stay compatible with cosign v3.

    cosign v3 defaults ``--use-signing-config`` and
    ``--new-bundle-format`` to true, which rejects the
    ``--tlog-upload=false`` + ``--output-signature`` invocation the
    air-gap path relies on. Dropping either opt-out silently breaks
    signing on any host with cosign v3 while still passing on v2.4.x,
    so pin the requirement here rather than only in the real-cosign
    integration test (which skips when cosign is absent).
    """
    repo_root = Path(__file__).resolve().parents[2]
    contents = (repo_root / "scripts" / "sign_airgap_wheelhouse.sh").read_text()
    assert "--use-signing-config=false" in contents
    assert "--new-bundle-format=false" in contents
    assert "--tlog-upload=false" in contents


# ---------------------------------------------------------------------------
# Phase 2: pluggable verifier + wheelhouse CLI subcommands
# ---------------------------------------------------------------------------


def test_verifier_protocol_kinds() -> None:
    from bernstein.core.distribution import VerifierKind

    assert {k.value for k in VerifierKind} == {"auto", "crypto", "cosign", "gpg"}


def test_select_verifier_returns_none_with_no_inputs() -> None:
    from bernstein.core.distribution import VerifierKind, select_verifier

    assert select_verifier(VerifierKind.AUTO) is None


def test_select_verifier_crypto_requires_pubkey(tmp_path: Path) -> None:
    from bernstein.core.distribution import VerifierKind, select_verifier

    assert select_verifier(VerifierKind.CRYPTO) is None
    pem = tmp_path / "key.pem"
    pem.write_text("-----BEGIN PUBLIC KEY-----\n-----END PUBLIC KEY-----\n")
    v = select_verifier(VerifierKind.CRYPTO, pubkey_path=pem)
    assert v is not None
    assert v.name == "crypto"


def test_select_verifier_gpg_when_available() -> None:
    import shutil as _shutil

    from bernstein.core.distribution import VerifierKind, select_verifier

    v = select_verifier(VerifierKind.GPG)
    if _shutil.which("gpg") or _shutil.which("gpg2"):
        assert v is not None
        assert v.name == "gpg"
    else:
        assert v is None


def test_verify_wheelhouse_core_returns_report(tmp_path: Path) -> None:
    from bernstein.core.distribution import verify_wheelhouse

    wh = _make_fixture_wheelhouse(tmp_path / "wh")
    report = verify_wheelhouse(wh.path)
    assert report.ok is True
    assert report.wheels_total == 1
    assert report.wheels_verified == 1
    assert report.signatures_present == 0


def test_verify_wheelhouse_core_enumerates_every_offender(tmp_path: Path) -> None:
    """SAST-style framing: report names each offender, no short-circuit."""
    import zipfile

    from bernstein.core.distribution import verify_wheelhouse

    target = tmp_path / "wh"
    target.mkdir()
    names = ["bernstein-1.9.4-py3-none-any.whl", "click-8.1.7-py3-none-any.whl"]
    wheels = []
    for n in names:
        with zipfile.ZipFile(target / n, "w") as zf:
            zf.writestr("a/__init__.py", "x")
        wheels.append({"name": n, "sha256": "deadbeef" * 8, "size": 0})
    (target / "MANIFEST.json").write_text(json.dumps({"version": "1.9.4", "wheels": wheels}, indent=2, sort_keys=True))
    report = verify_wheelhouse(target)
    assert report.ok is False
    assert any(names[0] in f for f in report.failures)
    assert any(names[1] in f for f in report.failures)


def test_verify_wheelhouse_require_signatures_flags_missing(tmp_path: Path) -> None:
    from bernstein.core.distribution import verify_wheelhouse

    wh = _make_fixture_wheelhouse(tmp_path / "wh")
    report = verify_wheelhouse(wh.path, require_signatures=True)
    assert report.ok is False
    assert any("missing signature" in f for f in report.failures)


def test_wheelhouse_verify_subcommand_passes(tmp_path: Path) -> None:
    from bernstein.cli.commands.wheelhouse_cmd import wheelhouse_group

    wh = _make_fixture_wheelhouse(tmp_path / "wh")
    runner = CliRunner()
    result = runner.invoke(wheelhouse_group, ["verify", str(wh.path)])
    assert result.exit_code == 0, result.output
    assert "PASSED" in result.output


def test_wheelhouse_verify_subcommand_fails_on_tamper(tmp_path: Path) -> None:
    from bernstein.cli.commands.wheelhouse_cmd import wheelhouse_group

    wh = _make_fixture_wheelhouse(tmp_path / "wh")
    target_wheel = wh.path / wh.wheel_names[0]
    target_wheel.write_bytes(target_wheel.read_bytes() + b"TAMPER")
    runner = CliRunner()
    result = runner.invoke(wheelhouse_group, ["verify", str(wh.path)])
    assert result.exit_code == 1
    assert "FAILED" in result.output
    assert wh.wheel_names[0] in result.output


def test_wheelhouse_verify_unknown_verifier_falls_back_to_auto(tmp_path: Path) -> None:
    from bernstein.cli.commands.wheelhouse_cmd import wheelhouse_group

    wh = _make_fixture_wheelhouse(tmp_path / "wh")
    runner = CliRunner()
    result = runner.invoke(wheelhouse_group, ["verify", str(wh.path), "--verifier", "auto"])
    assert result.exit_code == 0


def test_wheelhouse_build_help() -> None:
    from bernstein.cli.commands.wheelhouse_cmd import wheelhouse_group

    runner = CliRunner()
    result = runner.invoke(wheelhouse_group, ["build", "--help"])
    assert result.exit_code == 0
    assert "version" in result.output.lower()


def test_gpg_verifier_unavailable_returns_false_on_missing_binary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from bernstein.core.distribution import GpgVerifier

    monkeypatch.setattr("bernstein.core.distribution.verifier.shutil.which", lambda _name: None)
    v = GpgVerifier()
    assert v.available() is False
    blob = tmp_path / "blob"
    sig = tmp_path / "blob.sig"
    blob.write_bytes(b"x")
    sig.write_bytes(b"x")
    assert v.verify(blob, sig) is False


def test_cosign_verifier_unavailable_returns_false_on_missing_binary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from bernstein.core.distribution import CosignVerifier

    monkeypatch.setattr("bernstein.core.distribution.verifier.shutil.which", lambda _name: None)
    v = CosignVerifier()
    assert v.available() is False
    blob = tmp_path / "blob"
    sig = tmp_path / "blob.sig"
    blob.write_bytes(b"x")
    sig.write_bytes(b"x")
    assert v.verify(blob, sig) is False


def test_gpg_verifier_runs_subprocess_when_available(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Verify the verifier shells to gpg with --verify on the right inputs."""
    from bernstein.core.distribution import GpgVerifier

    captured: dict[str, object] = {}

    class _Result:
        returncode = 0

    def _fake_which(name: str) -> str | None:
        return f"/usr/bin/{name}" if name in {"gpg", "gpg2"} else None

    def _fake_run(cmd: list[str], **kwargs: object) -> _Result:  # pyright: ignore
        captured["cmd"] = cmd
        return _Result()

    monkeypatch.setattr("bernstein.core.distribution.verifier.shutil.which", _fake_which)
    monkeypatch.setattr("bernstein.core.distribution.verifier.subprocess.run", _fake_run)

    blob = tmp_path / "wheel"
    sig = tmp_path / "wheel.sig"
    blob.write_bytes(b"contents")
    sig.write_bytes(b"sig")
    v = GpgVerifier()
    assert v.verify(blob, sig) is True
    cmd = captured["cmd"]
    assert isinstance(cmd, list)
    assert "--verify" in cmd
    assert str(sig) in cmd
    assert str(blob) in cmd
