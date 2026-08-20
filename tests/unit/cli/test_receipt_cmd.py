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


def test_verify_says_whether_continuity_was_answered(workspace: Path):
    """The chain half of the same question, at the surface an operator reads."""
    assert _create(workspace, "bound.json", "--manifest-repo", str(workspace / "repo")).exit_code == 0
    runner = CliRunner()

    never_asked = runner.invoke(receipt_group, ["verify", str(workspace / "bound.json")])
    walked = runner.invoke(
        receipt_group,
        ["verify", str(workspace / "bound.json"), "--prev-digest", "genesis"],
    )

    assert never_asked.exit_code == 0, never_asked.output
    assert "chain: carried, NOT checked" in never_asked.output
    assert walked.exit_code == 0, walked.output
    assert "chain: checked against genesis" in walked.output


def test_the_two_verdicts_are_reported_independently(workspace: Path):
    """One flag must not move the other's bool -- they answer different questions."""
    assert _create(workspace, "bound.json", "--manifest-repo", str(workspace / "repo")).exit_code == 0
    runner = CliRunner()
    bundle = str(workspace / "bound.json")

    chain_only = json.loads(
        runner.invoke(receipt_group, ["verify", bundle, "--json", "--prev-digest", "genesis"]).output
    )
    manifest_only = json.loads(
        runner.invoke(
            receipt_group, ["verify", bundle, "--json", "--expected-manifest-digest", _digest(workspace)]
        ).output
    )

    assert chain_only["prev_digest_checked"] is True
    assert chain_only["manifest_digest_checked"] is False
    assert manifest_only["prev_digest_checked"] is False
    assert manifest_only["manifest_digest_checked"] is True


def test_json_separates_a_mismatched_anchor_from_one_never_compared(workspace: Path):
    """The state the human output cannot show, and the reason for the JSON key.

    Both runs pass ``--prev-digest``, so a flag echoed back would read ``true``
    for both, and both exit non-zero. Only the first one compared anything: a
    malformed chain link short-circuits the comparison inside step 6 without
    failing on the caller's behalf, so on the second the caller asked and the
    anchor was never looked at. The success branch cannot show this state --
    a malformed chain fails ``ok`` on its own -- which is why the JSON key
    carries more than the human line does.
    """
    assert _create(workspace, "wellformed.json").exit_code == 0
    spec = json.loads((workspace / "spec.json").read_text(encoding="utf-8"))
    spec["chain"] = {"anchor": "genesis", "length": 0}
    (workspace / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
    assert _create(workspace, "malformed.json").exit_code == 0
    runner = CliRunner()

    compared_and_diverged = json.loads(
        runner.invoke(
            receipt_group, ["verify", str(workspace / "wellformed.json"), "--json", "--prev-digest", "not-genesis"]
        ).output
    )
    never_compared = json.loads(
        runner.invoke(
            receipt_group, ["verify", str(workspace / "malformed.json"), "--json", "--prev-digest", "genesis"]
        ).output
    )

    assert compared_and_diverged["ok"] is False
    assert compared_and_diverged["prev_digest_checked"] is True
    assert [e["field"] for e in compared_and_diverged["errors"]] == ["chain.anchor"]

    assert never_compared["ok"] is False
    assert never_compared["prev_digest_checked"] is False, "a skipped comparison must not report as answered"
    assert [e["field"] for e in never_compared["errors"]] == ["chain.length"]


def test_a_manifest_that_cannot_be_read_refuses_instead_of_raising(workspace: Path, monkeypatch: pytest.MonkeyPatch):
    """load_manifest_from_repo guards with is_file() and then reads.

    Between those two calls the file can stop being readable -- a permission
    bit, a path that turns into a directory -- and what comes out is an OSError
    that is not FileNotFoundError. That is still a manifest the command could
    not load, so it gets the same clean refusal rather than escaping as a
    traceback.
    """
    import bernstein.core.volunteer.manifest as manifest_mod

    def unreadable(_repo_root: Path):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(manifest_mod, "load_manifest_from_repo", unreadable)

    result = _create(workspace, "never.json", "--manifest-repo", str(workspace / "repo"))

    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit), result.output
    assert "could not load manifest" in result.output
    assert not (workspace / "never.json").exists()


def test_manifest_repo_lets_the_spec_omit_the_digest(workspace: Path):
    """The contract widening the flag's help text now names.

    ResultBundle does no field validation, so the "" placeholder reaches
    bundle_with_manifest_digest and is replaced before signing -- but only when
    a manifest was supplied. Without the flag the field is still required.
    """
    spec = json.loads((workspace / "spec.json").read_text(encoding="utf-8"))
    del spec["manifest_sha256"]
    (workspace / "spec.json").write_text(json.dumps(spec), encoding="utf-8")

    bound = _create(workspace, "bound.json", "--manifest-repo", str(workspace / "repo"))
    assert bound.exit_code == 0, bound.output
    assert _digest(workspace) in bound.output

    loose = _create(workspace, "loose.json")
    assert loose.exit_code == 1
    assert "invalid spec" in loose.output
    assert not (workspace / "loose.json").exists()


def test_a_repo_without_a_manifest_fails_before_anything_is_signed(workspace: Path):
    (workspace / "empty").mkdir()
    result = _create(workspace, "never.json", "--manifest-repo", str(workspace / "empty"))
    assert result.exit_code == 1
    assert "could not load manifest" in result.output
    assert not (workspace / "never.json").exists(), "a bundle must not be written when the manifest is unusable"


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("not-json", b"\x00not json at all"),
        ("json-but-not-an-envelope", b'{"not": "a bundle"}'),
        ("envelope-missing-signatures", b'{"payload": "e30=", "payloadType": "application/vnd.in-toto+json"}'),
    ],
)
def test_malformed_input_refuses_cleanly_instead_of_raising(workspace: Path, name: str, payload: bytes):
    """#4109: ``load_bundle`` raises ``EnvelopeFormatError``, a ``RuntimeError``.

    The ``except`` clause around it caught ``(OSError, ValueError,
    json.JSONDecodeError)``, so every malformed shape escaped as an uncaught
    traceback rather than the refusal the clause is plainly written to emit.
    """
    bad = workspace / f"{name}.json"
    bad.write_bytes(payload)

    result = CliRunner().invoke(receipt_group, ["verify", str(bad)])

    assert result.exception is None or isinstance(result.exception, SystemExit), (
        f"{name}: expected a clean refusal, got {result.exception!r}"
    )
    assert result.exit_code == 1
    assert "could not parse bundle" in result.output


def test_a_signature_refusal_is_not_reported_as_a_parse_failure(workspace: Path):
    """The sibling DSSE errors stay uncaught at the load site on purpose.

    ``EnvelopeSignatureError`` means the bytes parsed and the signature did not
    verify. Folding it into the ``could not parse bundle`` arm would report a
    verification refusal as malformed input -- a different verdict, and the one
    a reader of the receipt reference page would be misled by.
    """
    from bernstein.core.security import audit_dsse

    assert issubclass(audit_dsse.EnvelopeFormatError, audit_dsse.DSSEError)
    assert issubclass(audit_dsse.EnvelopeSignatureError, audit_dsse.DSSEError)
    assert not issubclass(audit_dsse.EnvelopeSignatureError, audit_dsse.EnvelopeFormatError)

    created = _create(workspace, "bundle.json")
    assert created.exit_code == 0, created.output

    envelope = json.loads((workspace / "bundle.json").read_text(encoding="utf-8"))
    envelope["signatures"][0]["sig"] = "AA" * 32
    tampered = workspace / "tampered.json"
    tampered.write_text(json.dumps(envelope), encoding="utf-8")

    result = CliRunner().invoke(receipt_group, ["verify", str(tampered)])

    assert result.exit_code == 1
    assert "could not parse bundle" not in result.output
