"""CLI tests for ``bernstein receipt create|verify``, covering #3911.

The command surface had no tests at all before this file, so these cover the
round trip as well as the new manifest binding. The one that carries the issue
is :func:`test_a_spec_supplied_digest_does_not_survive_manifest_repo`: a spec
whose ``manifest_sha256`` is a placeholder produces a bundle that verifies
perfectly on its own and fails the moment a real policy digest is named -- the
defect #3911 closes, at the surface an operator actually types.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.cli.commands.receipt_cmd import receipt_group
from bernstein.core.volunteer.manifest import VOLUNTEER_MANIFEST_PATH, load_manifest_from_repo

_PLACEHOLDER = "0" * 64

_MANIFEST = {
    "version": 1,
    "license": "Apache-2.0",
    "gates": [["uv", "run", "pytest", "-q"]],
    "allowed_paths": ["src/**"],
    "egress_allowlist": ["pypi.org"],
    "sandbox": "microvm",
    "max_wall_clock_minutes": 30,
    "task_label": "volunteer-ok",
    "local_ok": True,
}


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A signing key, a spec carrying a placeholder digest, and a real manifest."""
    key = Ed25519PrivateKey.from_private_bytes(bytes([7]) * 32)
    (tmp_path / "worker.pem").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    manifest_path = tmp_path / "repo" / VOLUNTEER_MANIFEST_PATH
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(json.dumps(_MANIFEST), encoding="utf-8")
    (tmp_path / "spec.json").write_text(
        json.dumps(
            {
                "task": {"repo": "sipyourdrink-ltd/bernstein", "commit_sha": "abc123def456", "issue_number": 3911},
                "patch": "diff --git a/x b/x\n+hi\n",
                "gates": [{"command": "pytest -q", "exit_code": 0, "log": "ok\n"}],
                "manifest_sha256": _PLACEHOLDER,
                "adapter_id": "adapter.default.v3",
                "model_id": "claude-x",
                "sandbox_profile": "restricted-net-off",
                "selection_receipt": "sel-1",
                "created_at": "2026-08-17T00:00:00Z",
                "chain": {"anchor": "genesis", "length": 1},
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


def _create(workspace: Path, out: str, *extra: str):
    return CliRunner().invoke(
        receipt_group,
        [
            "create",
            str(workspace / "spec.json"),
            "--signing-key",
            str(workspace / "worker.pem"),
            "-o",
            str(workspace / out),
            *extra,
        ],
    )


def _digest(workspace: Path) -> str:
    return load_manifest_from_repo(workspace / "repo").digest


def test_create_without_manifest_repo_is_unchanged(workspace: Path):
    """Backward compatibility: a spec that names no manifest still works."""
    result = _create(workspace, "bundle.json")
    assert result.exit_code == 0, result.output

    payload = json.loads((workspace / "bundle.json").read_text(encoding="utf-8"))
    assert payload["payloadType"]
    verify = CliRunner().invoke(receipt_group, ["verify", str(workspace / "bundle.json")])
    assert verify.exit_code == 0, verify.output


def test_manifest_repo_derives_the_digest_the_spec_could_not(workspace: Path):
    result = _create(workspace, "bound.json", "--manifest-repo", str(workspace / "repo"))
    assert result.exit_code == 0, result.output
    assert _digest(workspace) in result.output
    assert _PLACEHOLDER not in (workspace / "bound.json").read_text(encoding="utf-8")


def test_a_spec_supplied_digest_does_not_survive_manifest_repo(workspace: Path):
    """The defect, at the CLI: a placeholder bundle verifies until a policy is named."""
    assert _create(workspace, "unbound.json").exit_code == 0

    # On its own it looks fine -- signature, patch and gate logs all check out.
    loose = CliRunner().invoke(receipt_group, ["verify", str(workspace / "unbound.json")])
    assert loose.exit_code == 0, loose.output
    assert "carried, NOT checked" in loose.output

    # Named against the project's real policy, it is exactly what it is.
    bound = CliRunner().invoke(
        receipt_group,
        ["verify", str(workspace / "unbound.json"), "--expected-manifest-digest", _digest(workspace)],
    )
    assert bound.exit_code == 1
    assert "manifest_sha256" in bound.output


def test_verify_says_which_of_the_two_happened(workspace: Path):
    """A bare ✓ must not read as a policy check."""
    assert _create(workspace, "bound.json", "--manifest-repo", str(workspace / "repo")).exit_code == 0
    runner = CliRunner()

    unchecked = runner.invoke(receipt_group, ["verify", str(workspace / "bound.json"), "--json"])
    checked = runner.invoke(
        receipt_group,
        ["verify", str(workspace / "bound.json"), "--json", "--expected-manifest-digest", _digest(workspace)],
    )

    assert json.loads(unchecked.output)["ok"] is json.loads(checked.output)["ok"] is True
    assert json.loads(unchecked.output)["manifest_digest_checked"] is False
    assert json.loads(checked.output)["manifest_digest_checked"] is True


def test_a_repo_without_a_manifest_fails_before_anything_is_signed(workspace: Path):
    (workspace / "empty").mkdir()
    result = _create(workspace, "never.json", "--manifest-repo", str(workspace / "empty"))
    assert result.exit_code == 1
    assert "could not load manifest" in result.output
    assert not (workspace / "never.json").exists(), "a bundle must not be written when the manifest is unusable"
