"""Witness co-signing of audit checkpoints (#3161).

``chain_checkpoint`` makes an audit-history shrink sticky only against
material the log host holds itself: an actor with write access to both the
chain segments and the checkpoints file can truncate both to a mutually
consistent earlier state and every local verification still passes. These
tests pin the behaviour of the witness that closes that residual - a second
party holding per-origin monotonic state, which co-signs a checkpoint only
when the tree is a consistent extension of what it last accepted.

Everything here is offline: real Ed25519 keys, real files, no network and no
witness server. The witness "host" is a state directory plus a private key.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

import pytest
from click.testing import CliRunner
from cryptography.hazmat.primitives.asymmetric import ed25519

from bernstein.cli.commands.audit_cmd import audit_group
from bernstein.core.persistence.chain_checkpoint import (
    checkpoints_path,
    record_checkpoint,
)
from bernstein.core.persistence.checkpoint_witness import (
    REFUSAL_INCONSISTENT_EXTENSION,
    REFUSAL_SIZE_REGRESSION,
    REFUSAL_STATE_MISMATCH,
    CosignatureFileError,
    WitnessRefusal,
    check_witness_contradictions,
    checkpoint_payload_sha256,
    cosign_checkpoint,
    cosignatures_path,
    load_cosignatures,
    load_witness_pin,
    record_cosignature,
    verify_cosignature,
    witness_state_path,
)
from bernstein.core.persistence.lineage_signer import (
    Ed25519FileKeySigner,
    Ed25519PublicKeyVerifier,
)
from bernstein.core.persistence.merkle import build_merkle_tree, compute_seal
from bernstein.core.security.audit import AuditLog

if TYPE_CHECKING:
    from pathlib import Path

_KEY = b"witness-substrate-test-key-012345"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed(tmp_path: Path, count: int = 6) -> Path:
    audit_dir = tmp_path / ".sdd" / "audit"
    log = AuditLog(audit_dir, key=_KEY)
    for i in range(count):
        log.log("test.event", "tester", "task", f"t-{i}", {"i": i})
    return audit_dir


def _seal_and_pin(audit_dir: Path) -> dict[str, Any]:
    _tree, seal = compute_seal(audit_dir, key=_KEY)
    return record_checkpoint(audit_dir, seal, key=_KEY)


def _truncate_history(audit_dir: Path, keep: int) -> None:
    """Drop all but the first *keep* records from the live segment.

    The chain stays HMAC-intact (a prefix of a chain is a valid chain), so
    only a pin held outside the truncated material can notice.
    """
    segment = next(iter(sorted(audit_dir.glob("*.jsonl"))))
    body = [line for line in segment.read_bytes().split(b"\n") if line.strip()]
    segment.write_bytes(b"".join(line + b"\n" for line in body[:keep]))


def _rebuild_root(checkpoint: dict[str, Any]) -> None:
    """Recompute ``root_hash`` in place so a hand-edited payload is self-consistent."""
    leaves = [(str(leaf["file"]), str(leaf["hash"])) for leaf in checkpoint["leaves"]]
    checkpoint["root_hash"] = build_merkle_tree(leaves, scheme=int(checkpoint.get("scheme", 2))).root.hash


def _witness_key(path: Path) -> Ed25519FileKeySigner:
    private = ed25519.Ed25519PrivateKey.generate()
    path.write_bytes(
        private.private_bytes_raw(),
    )
    path.chmod(0o600)
    return Ed25519FileKeySigner.from_path(path)


def _public_key_file(signer: Ed25519FileKeySigner, path: Path) -> Path:
    path.write_bytes(signer.public_key_bytes())
    return path


@pytest.fixture()
def witness(tmp_path: Path) -> tuple[Ed25519FileKeySigner, Path]:
    """A witness: an Ed25519 signer plus its own durable state directory."""
    return _witness_key(tmp_path / "witness.key"), tmp_path / "witness-state"


@pytest.fixture()
def cli_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project root with a seeded audit chain and the audit key on disk."""
    key_file = tmp_path / "audit.key"
    key_file.write_bytes(_KEY)
    key_file.chmod(0o600)
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(key_file))
    monkeypatch.setenv("COLUMNS", "200")
    _seed(tmp_path)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _cli_seal() -> None:
    result = CliRunner().invoke(audit_group, ["seal"])
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# 1. Co-signing and per-origin state
# ---------------------------------------------------------------------------


def test_witness_cosigns_a_checkpoint_and_pins_its_state(
    tmp_path: Path,
    witness: tuple[Ed25519FileKeySigner, Path],
) -> None:
    """A first acceptance co-signs the checkpoint and records the pin."""
    signer, state_dir = witness
    audit_dir = _seed(tmp_path)
    checkpoint = _seal_and_pin(audit_dir)

    result = cosign_checkpoint(state_dir, checkpoint, signer, witness_id="witness-a")

    assert result.bootstrapped is True
    assert result.cosignature.entry_count == checkpoint["entry_count"]
    assert result.cosignature.checkpoint_root == checkpoint["root_hash"]
    assert result.cosignature.payload_sha256 == checkpoint_payload_sha256(checkpoint)
    assert witness_state_path(state_dir, str(checkpoint["origin"])).exists()


def test_witness_state_survives_a_witness_restart(
    tmp_path: Path,
    witness: tuple[Ed25519FileKeySigner, Path],
) -> None:
    """The pin is durable: a fresh read sees what the previous run accepted."""
    signer, state_dir = witness
    audit_dir = _seed(tmp_path)
    checkpoint = _seal_and_pin(audit_dir)
    cosign_checkpoint(state_dir, checkpoint, signer)

    # A restart is a fresh read of the state directory - nothing in memory.
    pin = load_witness_pin(state_dir, str(checkpoint["origin"]))
    assert pin is not None
    assert pin.entry_count == checkpoint["entry_count"]
    assert pin.checkpoint_root == checkpoint["root_hash"]

    # And the second acceptance is no longer a bootstrap.
    AuditLog(audit_dir, key=_KEY).log("test.event", "tester", "task", "extra", {})
    later = cosign_checkpoint(state_dir, _seal_and_pin(audit_dir), signer)
    assert later.bootstrapped is False


def test_witness_without_prior_state_reports_that_it_is_bootstrapping(
    tmp_path: Path,
    witness: tuple[Ed25519FileKeySigner, Path],
) -> None:
    """Losing witness state degrades to the local-only guarantee, loudly.

    A witness whose state was deleted cannot refuse a rewound history - it has
    nothing to compare against. It must say so rather than co-sign in silence.
    """
    signer, state_dir = witness
    audit_dir = _seed(tmp_path)
    checkpoint = _seal_and_pin(audit_dir)
    cosign_checkpoint(state_dir, checkpoint, signer)

    witness_state_path(state_dir, str(checkpoint["origin"])).unlink()
    _truncate_history(audit_dir, keep=2)
    checkpoints_path(audit_dir).unlink()
    rewound = _seal_and_pin(audit_dir)

    result = cosign_checkpoint(state_dir, rewound, signer)
    assert result.bootstrapped is True
    assert result.cosignature.entry_count == rewound["entry_count"]


# ---------------------------------------------------------------------------
# 2. Refusals, distinct per cause
# ---------------------------------------------------------------------------


def test_witness_refuses_a_checkpoint_that_shrinks_the_history(
    tmp_path: Path,
    witness: tuple[Ed25519FileKeySigner, Path],
) -> None:
    """A smaller tree is a size regression, named as such."""
    signer, state_dir = witness
    audit_dir = _seed(tmp_path)
    cosign_checkpoint(state_dir, _seal_and_pin(audit_dir), signer)

    _truncate_history(audit_dir, keep=2)
    checkpoints_path(audit_dir).unlink()
    rewound = _seal_and_pin(audit_dir)

    with pytest.raises(WitnessRefusal) as excinfo:
        cosign_checkpoint(state_dir, rewound, signer)
    assert excinfo.value.reason == REFUSAL_SIZE_REGRESSION


def test_witness_refuses_a_fork_at_the_size_it_already_accepted(
    tmp_path: Path,
    witness: tuple[Ed25519FileKeySigner, Path],
) -> None:
    """Same size, different root: the submission does not match witness state."""
    signer, state_dir = witness
    audit_dir = _seed(tmp_path)
    checkpoint = _seal_and_pin(audit_dir)
    cosign_checkpoint(state_dir, checkpoint, signer)

    fork = json.loads(json.dumps(checkpoint))
    fork["leaves"][0]["hash"] = hashlib.sha256(b"forked").hexdigest()
    _rebuild_root(fork)

    with pytest.raises(WitnessRefusal) as excinfo:
        cosign_checkpoint(state_dir, fork, signer)
    assert excinfo.value.reason == REFUSAL_STATE_MISMATCH


def test_witness_refuses_a_larger_tree_whose_pinned_leaves_regressed(
    tmp_path: Path,
    witness: tuple[Ed25519FileKeySigner, Path],
) -> None:
    """Growth is not extension: a rewritten pinned segment fails consistency."""
    signer, state_dir = witness
    audit_dir = _seed(tmp_path)
    checkpoint = _seal_and_pin(audit_dir)
    cosign_checkpoint(state_dir, checkpoint, signer)

    forged = json.loads(json.dumps(checkpoint))
    forged["entry_count"] = int(checkpoint["entry_count"]) + 3
    forged["leaves"][0]["byte_len"] = int(forged["leaves"][0]["byte_len"]) - 1
    forged["leaves"][0]["hash"] = hashlib.sha256(b"rewritten").hexdigest()
    _rebuild_root(forged)

    with pytest.raises(WitnessRefusal) as excinfo:
        cosign_checkpoint(state_dir, forged, signer)
    assert excinfo.value.reason == REFUSAL_INCONSISTENT_EXTENSION


def test_witness_refuses_a_checkpoint_whose_leaves_do_not_rebuild_its_root(
    tmp_path: Path,
    witness: tuple[Ed25519FileKeySigner, Path],
) -> None:
    """A payload that is internally inconsistent never gets a co-signature."""
    signer, state_dir = witness
    audit_dir = _seed(tmp_path)
    checkpoint = _seal_and_pin(audit_dir)
    cosign_checkpoint(state_dir, checkpoint, signer)

    AuditLog(audit_dir, key=_KEY).log("test.event", "tester", "task", "extra", {})
    grown = json.loads(json.dumps(_seal_and_pin(audit_dir)))
    grown["root_hash"] = hashlib.sha256(b"claimed").hexdigest()

    with pytest.raises(WitnessRefusal) as excinfo:
        cosign_checkpoint(state_dir, grown, signer)
    assert excinfo.value.reason == REFUSAL_INCONSISTENT_EXTENSION


def test_a_refused_checkpoint_does_not_advance_witness_state(
    tmp_path: Path,
    witness: tuple[Ed25519FileKeySigner, Path],
) -> None:
    """A refusal leaves the pin exactly where it was."""
    signer, state_dir = witness
    audit_dir = _seed(tmp_path)
    checkpoint = _seal_and_pin(audit_dir)
    cosign_checkpoint(state_dir, checkpoint, signer)
    before = witness_state_path(state_dir, str(checkpoint["origin"])).read_bytes()

    _truncate_history(audit_dir, keep=2)
    checkpoints_path(audit_dir).unlink()
    with pytest.raises(WitnessRefusal):
        cosign_checkpoint(state_dir, _seal_and_pin(audit_dir), signer)

    assert witness_state_path(state_dir, str(checkpoint["origin"])).read_bytes() == before


# ---------------------------------------------------------------------------
# 3. Offline verification of the co-signature
# ---------------------------------------------------------------------------


def test_cosignature_verifies_offline_with_only_the_witness_public_key(
    tmp_path: Path,
    witness: tuple[Ed25519FileKeySigner, Path],
) -> None:
    """Keyless third-party verification: public key in, verdict out."""
    signer, state_dir = witness
    audit_dir = _seed(tmp_path)
    checkpoint = _seal_and_pin(audit_dir)
    cosig = cosign_checkpoint(state_dir, checkpoint, signer).cosignature

    verifier = Ed25519PublicKeyVerifier.from_path(_public_key_file(signer, tmp_path / "witness.pub"))
    assert verify_cosignature(cosig, verifier) == []


def test_a_cosignature_from_another_key_does_not_verify(
    tmp_path: Path,
    witness: tuple[Ed25519FileKeySigner, Path],
) -> None:
    """A self-asserted witness key cannot pass as the operator's witness."""
    signer, state_dir = witness
    audit_dir = _seed(tmp_path)
    checkpoint = _seal_and_pin(audit_dir)
    cosig = cosign_checkpoint(state_dir, checkpoint, signer).cosignature

    impostor = _witness_key(tmp_path / "impostor.key")
    verifier = Ed25519PublicKeyVerifier.from_path(_public_key_file(impostor, tmp_path / "impostor.pub"))
    assert verify_cosignature(cosig, verifier) != []


def test_recording_refuses_a_cosignature_the_witness_key_does_not_verify(
    tmp_path: Path,
    witness: tuple[Ed25519FileKeySigner, Path],
) -> None:
    """The log host stores nothing it could not check against the pinned key."""
    signer, state_dir = witness
    audit_dir = _seed(tmp_path)
    checkpoint = _seal_and_pin(audit_dir)
    cosig = cosign_checkpoint(state_dir, checkpoint, signer).cosignature

    impostor = _witness_key(tmp_path / "impostor.key")
    verifier = Ed25519PublicKeyVerifier.from_path(_public_key_file(impostor, tmp_path / "impostor.pub"))
    with pytest.raises(ValueError, match="signature"):
        record_cosignature(audit_dir, cosig, verifier)
    assert not cosignatures_path(audit_dir).exists()


def test_a_tampered_cosignature_line_fails_to_load(
    tmp_path: Path,
    witness: tuple[Ed25519FileKeySigner, Path],
) -> None:
    """A damaged co-signature file is not the same as an unwitnessed install."""
    signer, state_dir = witness
    audit_dir = _seed(tmp_path)
    checkpoint = _seal_and_pin(audit_dir)
    cosig = cosign_checkpoint(state_dir, checkpoint, signer).cosignature
    verifier = Ed25519PublicKeyVerifier.from_path(_public_key_file(signer, tmp_path / "witness.pub"))
    record_cosignature(audit_dir, cosig, verifier)

    cosignatures_path(audit_dir).write_bytes(b'{"version": 1, "kind": "nonsense"}\n')
    with pytest.raises(CosignatureFileError):
        load_cosignatures(audit_dir)


# ---------------------------------------------------------------------------
# 4. The load-bearing property: a mutually consistent rewind
# ---------------------------------------------------------------------------


def test_mutually_consistent_rewind_passes_locally_but_contradicts_the_witness(
    cli_workspace: Path,
    tmp_path: Path,
) -> None:
    """The whole point, with the contrast in one test.

    Rewinding the chain segments and the checkpoints file together leaves the
    local state self-consistent: ``bernstein audit verify`` passes. The same
    rewind checked against witness state is a hard verdict naming the
    witnessed checkpoint.
    """
    audit_dir = cli_workspace / ".sdd" / "audit"
    signer = _witness_key(cli_workspace / "witness.key")
    state_dir = cli_workspace / "witness-state"
    public_key = _public_key_file(signer, cli_workspace / "witness.pub")
    verifier = Ed25519PublicKeyVerifier.from_path(public_key)

    _cli_seal()
    from bernstein.core.persistence.chain_checkpoint import load_checkpoints

    checkpoint = load_checkpoints(audit_dir, _KEY).last
    assert checkpoint is not None
    cosig = cosign_checkpoint(state_dir, checkpoint, signer).cosignature
    record_cosignature(audit_dir, cosig, verifier)

    runner = CliRunner()
    assert runner.invoke(audit_group, ["verify", "--witness-key", str(public_key)]).exit_code == 0

    # The full local rollback: history shrinks, every local pin that would
    # have noticed is deleted with it, and the attacker re-seals so the
    # remaining local state is mutually consistent.
    _truncate_history(audit_dir, keep=2)
    checkpoints_path(audit_dir).unlink()
    cosignatures_path(audit_dir).unlink()
    _cli_seal()

    # Without a witness the rewind is clean - nothing local remembers.
    assert runner.invoke(audit_group, ["verify"]).exit_code == 0

    # Against the witness's own retained state it is a hard verdict that names
    # the checkpoint the witness accepted.
    result = runner.invoke(
        audit_group,
        ["verify", "--witness-key", str(public_key), "--witness-state", str(state_dir)],
    )
    assert result.exit_code == 1, result.output
    assert "Witness Contradiction" in result.output
    assert str(checkpoint["root_hash"])[:16] in result.output


def test_a_cosignature_whose_checkpoint_is_gone_is_a_contradiction(
    tmp_path: Path,
    witness: tuple[Ed25519FileKeySigner, Path],
) -> None:
    """Deleting the checkpoints file does not delete what the witness signed."""
    signer, state_dir = witness
    audit_dir = _seed(tmp_path)
    checkpoint = _seal_and_pin(audit_dir)
    cosig = cosign_checkpoint(state_dir, checkpoint, signer).cosignature

    conflicts = check_witness_contradictions(audit_dir, [cosig.claim()], checkpoint_roots=set())
    assert [c.kind for c in conflicts] == ["witness_checkpoint_missing"]


def test_verify_says_so_when_no_witness_cosignature_is_on_record(cli_workspace: Path) -> None:
    """An unwitnessed install passes and is told it is unwitnessed."""
    _cli_seal()
    result = CliRunner().invoke(audit_group, ["verify"])
    assert result.exit_code == 0, result.output
    assert "not witness" in result.output.lower()


# ---------------------------------------------------------------------------
# 5. The operator surface
# ---------------------------------------------------------------------------


def test_the_three_witness_commands_round_trip(cli_workspace: Path) -> None:
    """export on the log host, cosign on the witness, record back on the log host."""
    audit_dir = cli_workspace / ".sdd" / "audit"
    signer = _witness_key(cli_workspace / "witness.key")
    public_key = _public_key_file(signer, cli_workspace / "witness.pub")
    state_dir = cli_workspace / "witness-state"
    runner = CliRunner()
    _cli_seal()

    exported = cli_workspace / "cp.json"
    assert runner.invoke(audit_group, ["witness", "export", "--out", str(exported)]).exit_code == 0

    cosig = cli_workspace / "cosig.json"
    result = runner.invoke(
        audit_group,
        [
            "witness",
            "cosign",
            "--checkpoint",
            str(exported),
            "--key",
            str(cli_workspace / "witness.key"),
            "--state-dir",
            str(state_dir),
            "--out",
            str(cosig),
        ],
    )
    assert result.exit_code == 0, result.output
    # A bootstrap must not read as an endorsement of history before it.
    assert "no state for this chain" in result.output

    recorded = runner.invoke(
        audit_group,
        ["witness", "record", "--cosignature", str(cosig), "--witness-key", str(public_key)],
    )
    assert recorded.exit_code == 0, recorded.output
    assert load_cosignatures(audit_dir)

    verified = runner.invoke(audit_group, ["verify", "--witness-key", str(public_key)])
    assert verified.exit_code == 0, verified.output
    assert "Witness Verification Passed" in verified.output


def test_the_cosign_command_exits_non_zero_naming_the_refusal_cause(cli_workspace: Path) -> None:
    """A refusal is a failed command, not a warning buried in output."""
    audit_dir = cli_workspace / ".sdd" / "audit"
    signer = _witness_key(cli_workspace / "witness.key")
    state_dir = cli_workspace / "witness-state"
    runner = CliRunner()
    _cli_seal()

    from bernstein.core.persistence.chain_checkpoint import load_checkpoints

    checkpoint = load_checkpoints(audit_dir, _KEY).last
    assert checkpoint is not None
    cosign_checkpoint(state_dir, checkpoint, signer)

    _truncate_history(audit_dir, keep=2)
    checkpoints_path(audit_dir).unlink()
    _cli_seal()
    rewound = cli_workspace / "rewound.json"
    assert runner.invoke(audit_group, ["witness", "export", "--out", str(rewound)]).exit_code == 0

    result = runner.invoke(
        audit_group,
        [
            "witness",
            "cosign",
            "--checkpoint",
            str(rewound),
            "--key",
            str(cli_workspace / "witness.key"),
            "--state-dir",
            str(state_dir),
        ],
    )
    assert result.exit_code == 1, result.output
    assert REFUSAL_SIZE_REGRESSION in result.output
