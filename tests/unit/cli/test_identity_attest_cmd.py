"""CLI contracts for ``bernstein identity attest``.

These cover the surface this module owns: option validation, audit-directory
resolution, and that the group is reachable under ``identity``. Projection
semantics are covered by ``tests/unit/security/test_run_attestation_receipt.py``
and are deliberately not re-asserted here.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from bernstein.cli.commands import identity_attest_cmd
from bernstein.cli.commands.identity_cmd import identity_group
from bernstein.core.security import run_attestation_receipt
from bernstein.core.security.audit_chain import AuditChainStore
from tests.support.run_attestation import HMAC_KEY, anchored_provider, intent, kms


@pytest.fixture
def attest_cli_env(tmp_path: Path) -> tuple[Path, list[str], dict[str, str]]:
    """Create one real identity-anchored audit chain for CLI invocation."""
    provider = anchored_provider(tmp_path / ".sdd")
    asyncio.run(provider.prepare_dispatch(intent()))

    audit_key_path = tmp_path / "audit.key"
    audit_key_path.write_bytes(HMAC_KEY)
    audit_key_path.chmod(0o600)
    kms(tmp_path / "signing")
    signing_key_path = tmp_path / "signing" / "receipt-signing.pem"
    env = {"BERNSTEIN_AUDIT_KEY_PATH": str(audit_key_path)}
    common = [
        "--run",
        "run-1",
        "--signing-key-path",
        str(signing_key_path),
        "--workdir",
        str(tmp_path),
    ]
    return tmp_path, common, env


def test_attest_group_is_reachable_under_identity() -> None:
    assert "attest" in identity_group.commands
    attest = identity_group.commands["attest"]
    assert sorted(attest.commands) == ["show", "verify"]


def test_attest_verify_is_not_the_install_rev_verify() -> None:
    """``identity verify`` takes a token; ``identity attest verify`` takes --run.

    One noun, two objects. This asserts they stayed distinct commands rather
    than one growing an overloaded meaning.
    """
    install_rev_verify = identity_group.commands["verify"]
    attest_verify = identity_group.commands["attest"].commands["verify"]
    assert install_rev_verify is not attest_verify
    assert "run_id" in {param.name for param in attest_verify.params}
    assert "run_id" not in {param.name for param in install_rev_verify.params}


def test_signing_sources_are_mutually_exclusive(tmp_path: Path) -> None:
    signing_key = tmp_path / "key.pem"
    signing_key.write_text("unused because the options are mutually exclusive")
    result = CliRunner().invoke(
        identity_group,
        [
            "attest",
            "verify",
            "--run",
            "run-1",
            "--signing-key-path",
            str(signing_key),
            "--signing-env-var",
            "SOME_KEY",
            "--workdir",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


def test_missing_signing_key_path_uses_click_usage_contract(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        identity_group,
        [
            "attest",
            "verify",
            "--run",
            "run-1",
            "--signing-key-path",
            str(tmp_path / "missing.pem"),
            "--workdir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == identity_attest_cmd.EXIT_USAGE
    assert "does not exist" in result.output


def test_missing_signing_source_is_refused(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        identity_group,
        ["attest", "verify", "--run", "run-1", "--workdir", str(tmp_path)],
    )
    assert result.exit_code == 2
    assert "signing-key-path" in result.output


def test_missing_audit_directory_names_the_path(tmp_path: Path) -> None:
    """Fail closed with the resolved path rather than a traceback."""
    result = CliRunner().invoke(
        identity_group,
        [
            "attest",
            "show",
            "--run",
            "run-1",
            "--signing-env-var",
            "BERNSTEIN_TEST_ABSENT_KEY",
            "--workdir",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 1
    assert "Audit directory not found" in result.output or "signing key" in result.output


def test_verify_missing_audit_directory_leaves_no_default_output(tmp_path: Path) -> None:
    kms(tmp_path / "signing")
    signing_key = tmp_path / "signing" / "receipt-signing.pem"

    result = CliRunner().invoke(
        identity_group,
        [
            "attest",
            "verify",
            "--run",
            "run-1",
            "--signing-key-path",
            str(signing_key),
            "--workdir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == identity_attest_cmd.EXIT_FAILURE
    assert "Audit directory not found" in result.output
    assert not (tmp_path / ".sdd" / "evidence").exists()


def test_run_id_is_required(tmp_path: Path) -> None:
    result = CliRunner().invoke(identity_group, ["attest", "show", "--workdir", str(tmp_path)])
    assert result.exit_code == 2
    assert "--run" in result.output


def test_show_does_not_create_a_missing_audit_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A read-only projection must not mint a key and misreport tampering."""
    (tmp_path / ".sdd" / "audit").mkdir(parents=True)
    audit_key_path = tmp_path / "state" / "audit.key"
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(audit_key_path))
    monkeypatch.setattr(identity_attest_cmd, "_resolve_kms", lambda *_args: object())

    result = CliRunner().invoke(
        identity_group,
        ["attest", "show", "--run", "run-1", "--signing-env-var", "IGNORED", "--workdir", str(tmp_path)],
    )

    assert result.exit_code == identity_attest_cmd.EXIT_FAILURE
    assert "Failed to load audit key" in result.output
    assert "will not create key material" in result.output
    assert not audit_key_path.exists()


def test_real_chain_show_and_verify_emit(attest_cli_env: tuple[Path, list[str], dict[str, str]]) -> None:
    """Exercise both verbs against authenticated identity and dispatch evidence."""
    root, common, env = attest_cli_env
    runner = CliRunner()

    show = runner.invoke(identity_group, ["attest", "show", *common], env=env)
    assert show.exit_code == 0, show.output
    assert "dispatch evidence" in show.output
    assert "whole run" in show.output

    output_dir = root / "receipts"
    audit_before = {path.name: path.read_bytes() for path in (root / ".sdd" / "audit").glob("*") if path.is_file()}
    verify = runner.invoke(
        identity_group,
        ["attest", "verify", *common, "--output", str(output_dir)],
        env=env,
    )
    assert verify.exit_code == 0, verify.output
    assert "projection verified" in verify.output
    receipts = list(output_dir.glob("run-attestation-*.json"))
    assert len(receipts) == 1
    promoted_sha256 = hashlib.sha256(receipts[0].read_bytes()).hexdigest()
    assert promoted_sha256 in verify.output
    assert not list(output_dir.glob(".attest-*"))
    audit_after = {path.name: path.read_bytes() for path in (root / ".sdd" / "audit").glob("*") if path.is_file()}
    assert audit_after == audit_before


def test_through_hmac_reproduces_projection_after_chain_advances(
    attest_cli_env: tuple[Path, list[str], dict[str, str]],
) -> None:
    root, common, env = attest_cli_env
    chain = AuditChainStore(root / ".sdd" / "audit", key=HMAC_KEY)
    boundary = chain.query(include_archived=True)[-1].hmac
    runner = CliRunner()

    before = runner.invoke(
        identity_group,
        ["attest", "show", *common, "--through-hmac", boundary],
        env=env,
    )
    assert before.exit_code == 0, before.output

    chain.log(
        event_type="unrelated.audit.event",
        actor="operator",
        resource_type="system",
        resource_id="unrelated",
    )

    pinned = runner.invoke(
        identity_group,
        ["attest", "show", *common, "--through-hmac", boundary],
        env=env,
    )
    current = runner.invoke(identity_group, ["attest", "show", *common], env=env)
    output_dir = root / "pinned-receipts"
    verified = runner.invoke(
        identity_group,
        [
            "attest",
            "verify",
            *common,
            "--through-hmac",
            boundary,
            "--output",
            str(output_dir),
        ],
        env=env,
    )

    assert pinned.exit_code == 0, pinned.output
    assert pinned.output == before.output
    assert current.exit_code == 0, current.output
    assert current.output != pinned.output
    assert verified.exit_code == 0, verified.output
    receipt_path = next(output_dir.glob("run-attestation-*.json"))
    receipt = json.loads(receipt_path.read_text())
    assert receipt["run_attestation"]["through_hmac"] == boundary


def test_promoted_digest_mismatch_fails_and_removes_receipt(
    attest_cli_env: tuple[Path, list[str], dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, common, env = attest_cli_env
    output_dir = root / "receipts"
    monkeypatch.setattr(identity_attest_cmd, "_sha256_file", lambda _path: "0" * 64)

    result = CliRunner().invoke(
        identity_group,
        ["attest", "verify", *common, "--output", str(output_dir)],
        env=env,
    )

    assert result.exit_code == identity_attest_cmd.EXIT_FAILURE
    assert "Promoted receipt bytes differ" in result.output
    assert not list(output_dir.glob("*.json"))
    assert not list(output_dir.glob(".attest-*"))


def test_promoted_receipt_read_failure_removes_receipt(
    attest_cli_env: tuple[Path, list[str], dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, common, env = attest_cli_env
    output_dir = root / "receipts"

    def _fail_read(_path: Path) -> str:
        raise OSError("forced durable read failure")

    monkeypatch.setattr(identity_attest_cmd, "_sha256_file", _fail_read)

    result = CliRunner().invoke(
        identity_group,
        ["attest", "verify", *common, "--output", str(output_dir)],
        env=env,
    )

    assert result.exit_code == identity_attest_cmd.EXIT_FAILURE
    assert "Failed to re-hash promoted receipt" in result.output
    assert not list(output_dir.glob("*.json"))
    assert not list(output_dir.glob(".attest-*"))


def test_identity_help_names_attest_operator_surface() -> None:
    result = CliRunner().invoke(identity_group, ["--help"])

    assert result.exit_code == 0
    assert "identity attest show" in result.output


def test_real_chain_tamper_exits_nonzero_and_names_entry(
    attest_cli_env: tuple[Path, list[str], dict[str, str]],
) -> None:
    """Physically mutate the chain and require an entry-specific refusal."""
    root, common, env = attest_cli_env
    segment = next((root / ".sdd" / "audit").glob("*.jsonl"))
    rows = segment.read_text().splitlines()
    first = json.loads(rows[0])
    first["actor"] = f"tampered-{first['actor']}"
    rows[0] = json.dumps(first, sort_keys=True, separators=(",", ":"))
    segment.write_text("\n".join(rows) + "\n")

    result = CliRunner().invoke(identity_group, ["attest", "verify", *common], env=env)

    assert result.exit_code == identity_attest_cmd.EXIT_FAILURE
    assert "source audit chain verification failed" in result.output
    assert ".jsonl:1:" in result.output
    assert not list((root / ".sdd" / "evidence").glob("*.json"))


def test_failed_semantic_verification_is_not_promoted(
    attest_cli_env: tuple[Path, list[str], dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected projection must not look like normal verified evidence."""
    root, common, env = attest_cli_env
    output_dir = root / "receipts"
    monkeypatch.setattr(
        run_attestation_receipt,
        "verify_run_attestation_projection",
        lambda _receipt: SimpleNamespace(ok=False, errors=("forced semantic failure",)),
    )

    result = CliRunner().invoke(
        identity_group,
        ["attest", "verify", *common, "--output", str(output_dir)],
        env=env,
    )

    assert result.exit_code == identity_attest_cmd.EXIT_FAILURE
    assert "forced semantic failure" in result.output
    assert not list(output_dir.glob("*.json"))
    assert not list(output_dir.glob(".attest-*"))
