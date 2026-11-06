"""Chain-anchored, offline-recomputable merge-probe receipts (#3279 step 3).

The property under test is that the receipt is worth something to somebody who
does not have us. A probe result is only evidence if a third party holding the
repository and stock git can re-derive the recorded tree id from the receipt
alone, and can tell a tampered receipt from a repository whose merge
configuration simply moved.

So these cover:

* the receipt is deterministic -- two operators probing the same pair mint the
  same ``receipt_hash``, because the clock is outside the signed body;
* a mutated body fails signature verification, whichever field moved;
* re-derivation against the real repository reproduces the tree id, and a
  forged tree id is caught;
* merge-configuration drift is a *distinct* outcome from a mismatch, because
  editing ``.gitattributes`` is not forgery;
* the chain entry pins exactly this receipt by content hash.

Like the step-1 probe tests these run against a real ``git`` binary in
``tmp_path``. Nothing here mocks git plumbing: the point of the receipt is
that stock git reproduces it, so a stubbed subprocess would assert nothing
worth knowing. They never touch a remote and never reach the network.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from bernstein.core.git.merge_probe_receipt import (
    MERGE_PROBE_DOMAIN,
    MERGE_PROBE_KID,
    MERGE_PROBE_RECEIPT_VERSION,
    VERIFY_CONFIG_DRIFT,
    VERIFY_OK,
    VERIFY_SIGNATURE_INVALID,
    VERIFY_TREE_MISMATCH,
    MergeProbeReceipt,
    build_probe_receipt,
    probe_command,
    read_probe_receipt,
    receipt_from_probe,
    rederive_probe_receipt,
    verify_probe_receipt,
    write_probe_receipt,
)
from bernstein.core.git.merge_tree_probe import (
    PROBE_MIN_GIT_VERSION,
    ProbeVerdict,
    git_version,
    probe_integration,
    supports_write_tree,
)

# ---------------------------------------------------------------------------
# Helpers -- mirrored from tests/unit/git/test_merge_tree_probe.py so both
# suites exercise the same real repository shape.
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        msg = f"git {' '.join(args)} failed ({result.returncode}): {result.stderr.strip()}"
        raise AssertionError(msg)
    return result.stdout


def _write(repo: Path, rel: str, body: str) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8", newline="\n")


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Base commit with two colliding workers and one disjoint worker."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main", ".")
    _git(root, "config", "user.email", "probe@example.test")
    _git(root, "config", "user.name", "Probe Fixture")
    _git(root, "config", "core.autocrlf", "false")

    _write(root, "shared.txt", "line1\nline2\nline3\n")
    base = _commit(root, "base")

    _git(root, "checkout", "-q", "-b", "worker/a", base)
    _write(root, "shared.txt", "AAA\nline2\nline3\n")
    _write(root, "only_a.txt", "from a\n")
    _commit(root, "a")

    _git(root, "checkout", "-q", "-b", "worker/b", base)
    _write(root, "shared.txt", "BBB\nline2\nline3\n")
    _commit(root, "b")

    _git(root, "checkout", "-q", "-b", "worker/c", base)
    _write(root, "only_c.txt", "from c\n")
    _commit(root, "c")

    _git(root, "checkout", "-q", "main")
    return root


@pytest.fixture(autouse=True)
def _require_write_tree(repo: Path) -> None:
    version = git_version(repo)
    if not supports_write_tree(version):
        pytest.skip(f"git {version} predates {PROBE_MIN_GIT_VERSION}; probing is unavailable by design")


@pytest.fixture
def keys() -> dict[str, str]:
    from bernstein.core.lineage.identity import generate_keypair

    private_key, public_key = generate_keypair()
    return {"private": private_key, "public": public_key}


def _sealed(repo: Path, keys: dict[str, str], a: str = "worker/a", b: str = "worker/c") -> MergeProbeReceipt:
    probe = probe_integration(a, b, repo)
    return build_probe_receipt(
        probe, private_key_pem=keys["private"], public_key_pem=keys["public"], timestamp=1700000000
    )


# ---------------------------------------------------------------------------
# Determinism and identity
# ---------------------------------------------------------------------------


def test_two_operators_probing_one_pair_mint_the_same_receipt_hash(repo: Path, keys: dict[str, str]) -> None:
    """The clock is outside the signed body, so the hash is a content identity.

    If the timestamp were in the body, two operators checking the same pair
    would derive different hashes for the same fact and the receipt would
    identify the machine rather than the measurement.
    """
    first = _sealed(repo, keys)
    second = build_probe_receipt(
        probe_integration("worker/a", "worker/c", repo),
        private_key_pem=keys["private"],
        public_key_pem=keys["public"],
        timestamp=1900000000,
    )

    assert first.timestamp != second.timestamp
    assert first.receipt_hash() == second.receipt_hash()
    assert first.canonical_bytes() == second.canonical_bytes()


def test_the_signed_body_excludes_the_clock_and_the_signature(repo: Path, keys: dict[str, str]) -> None:
    receipt = _sealed(repo, keys)
    body = receipt.to_canonical_dict()

    assert "timestamp" not in body
    assert "signature" not in body
    assert "signer_public_key_pem" not in body
    # ...while the full on-disk record carries all three plus the hash.
    row = receipt.to_dict()
    assert {"timestamp", "signature", "signer_public_key_pem", "receipt_hash"} <= set(row)


def test_the_pair_is_ordered(repo: Path, keys: dict[str, str]) -> None:
    """Swapping the sides is a different probe and must be a different receipt."""
    forward = _sealed(repo, keys, "worker/a", "worker/c")
    reverse = _sealed(repo, keys, "worker/c", "worker/a")

    assert forward.receipt_hash() != reverse.receipt_hash()


def test_the_receipt_records_the_command_a_verifier_re_runs(repo: Path, keys: dict[str, str]) -> None:
    """Verbatim, not a digest: re-derivation should be copy-paste."""
    receipt = _sealed(repo, keys)

    assert receipt.command == probe_command(receipt.a_commit, receipt.b_commit)
    assert receipt.command.startswith("git merge-tree --write-tree --name-only -z ")
    assert receipt.a_commit in receipt.command
    assert receipt.b_commit in receipt.command


def test_a_clean_probe_is_recorded_as_textual_clean_never_safe(repo: Path, keys: dict[str, str]) -> None:
    """No code path may spell a claim stronger than what was measured."""
    receipt = _sealed(repo, keys, "worker/a", "worker/c")

    assert receipt.verdict == ProbeVerdict.TEXTUAL_CLEAN
    assert "SAFE" not in json.dumps(receipt.to_dict())


def test_a_conflicting_pair_records_the_conflict_and_still_names_a_tree(repo: Path, keys: dict[str, str]) -> None:
    receipt = _sealed(repo, keys, "worker/a", "worker/b")

    assert receipt.verdict == ProbeVerdict.CONFLICTED
    # The conflicted result is still a real object, so it is still anchorable.
    assert receipt.tree_id
    assert receipt.conflicted_paths_digest


# ---------------------------------------------------------------------------
# Signature
# ---------------------------------------------------------------------------


def test_a_sealed_receipt_verifies(repo: Path, keys: dict[str, str]) -> None:
    assert verify_probe_receipt(_sealed(repo, keys)) is True


def test_an_unsigned_receipt_does_not_verify(repo: Path) -> None:
    """A receipt anchoring a hash nobody can attribute is not evidence."""
    unsigned = receipt_from_probe(probe_integration("worker/a", "worker/c", repo))

    assert unsigned.signature == ""
    assert verify_probe_receipt(unsigned) is False


@pytest.mark.parametrize("field", ["tree_id", "a_commit", "verdict", "merge_config_digest", "command"])
def test_mutating_any_signed_field_breaks_the_signature(repo: Path, keys: dict[str, str], field: str) -> None:
    """Every field in the canonical body is actually covered by the signature."""
    receipt = _sealed(repo, keys)
    row = receipt.to_dict()
    row[field] = "tampered"

    assert verify_probe_receipt(MergeProbeReceipt.from_dict(row)) is False


def test_the_signature_is_domain_separated(repo: Path, keys: dict[str, str]) -> None:
    """A signature over these bytes cannot be replayed as another subsystem's."""
    receipt = _sealed(repo, keys)

    assert receipt.signing_input().startswith(MERGE_PROBE_DOMAIN)
    assert receipt.signing_input() == MERGE_PROBE_DOMAIN + receipt.canonical_bytes()


# ---------------------------------------------------------------------------
# Offline re-derivation -- the reason the receipt exists
# ---------------------------------------------------------------------------


def test_a_third_party_re_derives_the_recorded_tree_id(repo: Path, keys: dict[str, str]) -> None:
    """The acceptance bar: stock git reproduces the receipt, no bernstein needed."""
    receipt = _sealed(repo, keys)

    result = rederive_probe_receipt(receipt, repo)

    assert result.status == VERIFY_OK
    assert result.ok
    assert result.recomputed_tree_id == receipt.tree_id

    # And the same tree id falls out of the recorded command run by hand.
    by_hand = _git(repo, "merge-tree", "--write-tree", "--name-only", "-z", receipt.a_commit, receipt.b_commit)
    assert by_hand.split("\0", 1)[0].strip() == receipt.tree_id


def test_a_forged_tree_id_is_caught(repo: Path, keys: dict[str, str]) -> None:
    """Re-derivation is the check; the recorded value is never trusted.

    The forgery is properly signed, so only re-running the probe can refute
    it. A receipt that merely verified its own signature would pass this.
    """
    from bernstein.core.lineage.identity import sign_detached

    honest = receipt_from_probe(probe_integration("worker/a", "worker/c", repo))
    lie = MergeProbeReceipt.from_dict({**honest.to_dict(), "tree_id": "0" * 40})
    signature = sign_detached(lie.signing_input(), keys["private"], kid=MERGE_PROBE_KID)
    forged = MergeProbeReceipt.from_dict(
        {**lie.to_dict(), "signature": signature, "signer_public_key_pem": keys["public"]}
    )

    assert verify_probe_receipt(forged) is True  # the lie is properly signed
    result = rederive_probe_receipt(forged, repo)
    assert result.status == VERIFY_TREE_MISMATCH
    assert not result.ok
    assert result.recomputed_tree_id != forged.tree_id


def test_an_unsigned_body_is_refused_before_re_derivation(repo: Path, keys: dict[str, str]) -> None:
    """Re-deriving a body nobody signed answers the wrong question."""
    receipt = _sealed(repo, keys)
    stripped = MergeProbeReceipt.from_dict({**receipt.to_dict(), "signature": ""})

    result = rederive_probe_receipt(stripped, repo)

    assert result.status == VERIFY_SIGNATURE_INVALID
    assert not result.ok


def test_merge_config_drift_is_reported_apart_from_a_mismatch(repo: Path, keys: dict[str, str]) -> None:
    """Editing .gitattributes is not forgery, and must not be reported as one.

    This is the acceptance criterion that a recorded merge-config change is a
    distinct outcome: the operator's next move is to restore the
    configuration, not to investigate a tampered receipt.
    """
    receipt = _sealed(repo, keys)
    _write(repo, ".gitattributes", "*.txt merge=union\n")
    _commit(repo, "add merge driver")

    result = rederive_probe_receipt(receipt, repo)

    assert result.status == VERIFY_CONFIG_DRIFT
    assert not result.ok
    assert "merge configuration moved" in result.detail


# ---------------------------------------------------------------------------
# Chain anchoring and persistence
# ---------------------------------------------------------------------------


def test_the_chain_entry_pins_this_receipt_by_content_hash(repo: Path, keys: dict[str, str], tmp_path: Path) -> None:
    from bernstein.core.git.merge_probe_receipt import anchor_probe_receipt
    from bernstein.core.security.audit_chain import EVENT_MERGE_PROBE, AuditChainStore

    receipt = _sealed(repo, keys)
    chain = AuditChainStore(tmp_path / "runtime")

    anchor_probe_receipt(chain, receipt)

    recorded = list(chain.query(event_type=EVENT_MERGE_PROBE))
    assert len(recorded) == 1
    assert recorded[0].details["receipt_hash"] == receipt.receipt_hash()
    assert recorded[0].details["tree_id"] == receipt.tree_id
    assert recorded[0].details["verdict"] == receipt.verdict


def test_the_receipt_round_trips_through_disk(repo: Path, keys: dict[str, str], tmp_path: Path) -> None:
    receipt = _sealed(repo, keys)

    path = write_probe_receipt(tmp_path / ".sdd", receipt)
    recovered = read_probe_receipt(path)

    assert recovered is not None
    assert recovered.to_dict() == receipt.to_dict()
    assert verify_probe_receipt(recovered) is True
    # Content-addressed: the file name is the anchor, so rewriting is idempotent.
    assert path.stem == receipt.receipt_hash().split(":", 1)[-1]
    assert write_probe_receipt(tmp_path / ".sdd", receipt) == path


def test_a_missing_or_malformed_record_reads_as_none(tmp_path: Path) -> None:
    assert read_probe_receipt(tmp_path / "absent.json") is None
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert read_probe_receipt(broken) is None


def test_the_receipt_states_its_wire_version(repo: Path, keys: dict[str, str]) -> None:
    """Version travels inside the signed bytes, not beside them."""
    receipt = _sealed(repo, keys)

    assert receipt.v == MERGE_PROBE_RECEIPT_VERSION
    assert receipt.to_canonical_dict()["v"] == MERGE_PROBE_RECEIPT_VERSION
