"""Chain-anchored, offline-recomputable receipt for a merge-tree probe (#3279 step 3).

:mod:`bernstein.core.git.merge_tree_probe` (step 1) produces the measurement:
what two live worker commits compose to, named by the content of the merged
tree. This module turns that measurement into an artefact somebody else can
check without us.

The verification story decides the design. A third party is handed one JSON
file. They check out the two recorded commit ids, run the recorded command
under the recorded git version, and compare the tree id they get. Equal means
the receipt told the truth about what those two worktrees composed to at that
moment. Unequal means either the receipt is wrong or the repository is not the
one the run saw. There is no bernstein-shaped step anywhere in that argument,
which is the whole reason the entry is worth signing::

    git merge-tree --write-tree --name-only -z <a_commit> <b_commit>

The command is recorded verbatim rather than as a digest. A digest would prove
the receipt knew which command it meant while still leaving the verifier to
reconstruct it from prose; the point is that re-derivation is copy-paste, not
reimplementation.

What is excluded from the signed body
-------------------------------------
``timestamp`` and the signature itself, exactly as
:mod:`bernstein.core.git.read_set_receipt` excludes them. Two operators who
probe the same pair under the same git and the same merge configuration derive
the same ``receipt_hash``. A clock in the body would make that false and turn
a reproducible artefact into a per-machine one.

Merge-configuration drift is not tampering
------------------------------------------
``.gitattributes`` merge drivers and the rename-detection knobs change what a
merge produces. When they move, an honest receipt stops re-deriving -- and
saying "mismatch" there would accuse an operator of forging a receipt when
they merely changed a config file. :data:`VERIFY_CONFIG_DRIFT` is therefore a
distinct outcome from :data:`VERIFY_TREE_MISMATCH`, and a verifier must report
it as such.

Scope
-----
Step 3 of issue #3279 only: build, sign, anchor, persist, re-derive. Probe
scheduling (step 2), the deterministic divergence response (step 4) and the
``bernstein audit verify`` wiring (step 5) are separate changes. Nothing in
the tree calls this yet.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.git.merge_tree_probe import ProbeVerdict, probe_integration
from bernstein.core.lineage.identity import AgentCard, sign_detached, verify_detached

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.git.merge_tree_probe import MergeTreeProbe
    from bernstein.core.security.audit import AuditEvent
    from bernstein.core.security.audit_chain import AuditChainStore

__all__ = [
    "MERGE_PROBE_DOMAIN",
    "MERGE_PROBE_KID",
    "MERGE_PROBE_RECEIPT_VERSION",
    "VERIFY_CONFIG_DRIFT",
    "VERIFY_OK",
    "VERIFY_PATHS_MISMATCH",
    "VERIFY_SIGNATURE_INVALID",
    "VERIFY_TREE_MISMATCH",
    "VERIFY_UNVERIFIABLE",
    "MergeProbeReceipt",
    "ProbeVerification",
    "anchor_probe_receipt",
    "build_probe_receipt",
    "probe_command",
    "read_probe_receipt",
    "receipt_from_probe",
    "rederive_probe_receipt",
    "verify_probe_receipt",
    "write_probe_receipt",
]

#: Wire-format version stamped into every receipt preimage.
MERGE_PROBE_RECEIPT_VERSION = 1

#: Domain-separation tag prefixed to the signed bytes, so a signature over
#: ``DOMAIN || canonical`` cannot be replayed as any other subsystem's JWS.
MERGE_PROBE_DOMAIN = b"bernstein.merge-probe.v1\x00"

#: Key id carried in the detached JWS protected header.
MERGE_PROBE_KID = "merge-probe"


def probe_command(a_commit: str, b_commit: str) -> str:
    """Return the exact command a verifier re-runs, as a single string.

    Built here rather than inlined at the call site so the receipt and the
    probe can never drift into describing different commands.
    """
    return f"git merge-tree --write-tree --name-only -z {a_commit} {b_commit}"


# ---------------------------------------------------------------------------
# Re-derivation outcomes
# ---------------------------------------------------------------------------

#: Every recorded value reproduced from the repository.
VERIFY_OK = "ok"

#: The recomputed tree id differs. The receipt does not describe what these
#: two commits compose to in this repository.
VERIFY_TREE_MISMATCH = "tree_mismatch"

#: The tree id reproduced but the conflicted-path digest did not.
VERIFY_PATHS_MISMATCH = "paths_mismatch"

#: The merge configuration moved since the receipt was minted, so the recorded
#: result is re-derivable only under its own recorded digest. Reported apart
#: from a mismatch because the operator's next move is different: restore the
#: configuration, not investigate a forgery.
VERIFY_CONFIG_DRIFT = "config_drift"

#: The detached signature does not verify against the embedded key.
VERIFY_SIGNATURE_INVALID = "signature_invalid"

#: Re-derivation could not run at all -- git missing or too old, or the
#: commits are absent from this repository. Distinct from a mismatch: nothing
#: about the receipt was contradicted.
VERIFY_UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True, slots=True)
class ProbeVerification:
    """Outcome of re-deriving a probe receipt against a repository.

    Attributes:
        status: One of the ``VERIFY_*`` constants.
        detail: Human-readable description; empty when :data:`VERIFY_OK`.
        recomputed_tree_id: What re-derivation actually produced, when it ran.
    """

    status: str
    detail: str = ""
    recomputed_tree_id: str = ""

    @property
    def ok(self) -> bool:
        """Whether the receipt fully reproduced.

        Deliberately False for :data:`VERIFY_CONFIG_DRIFT` and
        :data:`VERIFY_UNVERIFIABLE`. Neither says the receipt is wrong, and
        neither says it is right; a caller that wants to treat them leniently
        has to name them, so nobody gets that by writing ``if result.ok``.
        """
        return self.status == VERIFY_OK


# ---------------------------------------------------------------------------
# The receipt
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MergeProbeReceipt:
    """A signed, chain-anchorable statement of what two worker commits composed to.

    The signature is detached (RFC 7515 / RFC 7797): the signed body is the
    canonical dict from :meth:`to_canonical_dict`, and the receipt carries the
    signer's public key so a verifier holding only the receipt can check it.

    Attributes:
        v: Wire-format version.
        a_commit: Resolved object id of the first side (ours).
        b_commit: Resolved object id of the second side (theirs).
        merge_base: Resolved merge base, recorded for re-derivation.
        tree_id: Object id of the merged tree -- the merge itself, named by
            its content. Empty only when the probe was ``UNAVAILABLE``.
        conflicted_paths_digest: ``sha256:`` digest over the canonical JSON of
            the sorted conflicted paths. Always present, so a clean probe
            carries the digest of the empty list rather than a special case.
        verdict: ``TEXTUAL_CLEAN``, ``CONFLICTED`` or ``UNAVAILABLE``. Never
            ``SAFE``: a clean probe is a statement about textual composition
            only, and no consumer may spell a stronger claim than was measured.
        reason: Empty unless the verdict is ``UNAVAILABLE``.
        git_version: Dotted version of the git that produced the result.
        merge_config_digest: Digest of the merge configuration in force.
        command: The verbatim command a third party re-runs.
        timestamp: Unix seconds the receipt was minted. Excluded from the
            canonical body so two operators probing the same pair derive the
            same ``receipt_hash``.
        signer_public_key_pem: The install's Ed25519 public key (PEM).
        signature: Detached JWS over ``DOMAIN || canonical_bytes``.
    """

    v: int
    a_commit: str
    b_commit: str
    merge_base: str
    tree_id: str
    conflicted_paths_digest: str
    verdict: str
    reason: str
    git_version: str
    merge_config_digest: str
    command: str
    timestamp: int = 0
    signer_public_key_pem: str = ""
    signature: str = ""

    def to_canonical_dict(self) -> dict[str, Any]:
        """Return the deterministic signed body (excludes signature + clock)."""
        return {
            "v": self.v,
            "a_commit": self.a_commit,
            "b_commit": self.b_commit,
            "merge_base": self.merge_base,
            "tree_id": self.tree_id,
            "conflicted_paths_digest": self.conflicted_paths_digest,
            "verdict": self.verdict,
            "reason": self.reason,
            "git_version": self.git_version,
            "merge_config_digest": self.merge_config_digest,
            "command": self.command,
        }

    def canonical_bytes(self) -> bytes:
        """RFC 8785-style canonical bytes of the signed body."""
        return json.dumps(
            self.to_canonical_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")

    def receipt_hash(self) -> str:
        """``sha256:`` content hash of the canonical body (the chain anchor)."""
        return "sha256:" + hashlib.sha256(self.canonical_bytes()).hexdigest()

    def signing_input(self) -> bytes:
        """Domain-separated preimage the detached JWS is computed over."""
        return MERGE_PROBE_DOMAIN + self.canonical_bytes()

    def to_dict(self) -> dict[str, Any]:
        """Full on-disk record (signed body + signature + clock + hash)."""
        row = self.to_canonical_dict()
        row["timestamp"] = self.timestamp
        row["signer_public_key_pem"] = self.signer_public_key_pem
        row["signature"] = self.signature
        row["receipt_hash"] = self.receipt_hash()
        return row

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> MergeProbeReceipt:
        """Reconstruct a receipt from its on-disk representation.

        Every field is coerced rather than trusted: the file may have been
        edited, and a loader that raises on a malformed value reports a crash
        where the caller is owed a verdict.
        """
        return cls(
            v=int(row.get("v", MERGE_PROBE_RECEIPT_VERSION)),
            a_commit=str(row.get("a_commit", "")),
            b_commit=str(row.get("b_commit", "")),
            merge_base=str(row.get("merge_base", "")),
            tree_id=str(row.get("tree_id", "")),
            conflicted_paths_digest=str(row.get("conflicted_paths_digest", "")),
            verdict=str(row.get("verdict", "")),
            reason=str(row.get("reason", "")),
            git_version=str(row.get("git_version", "")),
            merge_config_digest=str(row.get("merge_config_digest", "")),
            command=str(row.get("command", "")),
            timestamp=int(row.get("timestamp", 0)),
            signer_public_key_pem=str(row.get("signer_public_key_pem", "")),
            signature=str(row.get("signature", "")),
        )


def receipt_from_probe(probe: MergeTreeProbe) -> MergeProbeReceipt:
    """Project a probe into an unsigned receipt.

    A separate step from signing so a caller can derive the ``receipt_hash``
    of a probe without holding a key -- which is what makes the hash usable as
    an identity rather than as a credential.
    """
    return MergeProbeReceipt(
        v=MERGE_PROBE_RECEIPT_VERSION,
        a_commit=probe.a_commit,
        b_commit=probe.b_commit,
        merge_base=probe.merge_base,
        tree_id=probe.tree_id,
        conflicted_paths_digest=probe.conflicted_paths_digest,
        verdict=str(probe.verdict),
        reason=probe.reason,
        git_version=".".join(str(part) for part in probe.git_version),
        merge_config_digest=probe.merge_config_digest,
        command=probe_command(probe.a_commit, probe.b_commit),
    )


def build_probe_receipt(
    probe: MergeTreeProbe,
    *,
    private_key_pem: str,
    public_key_pem: str,
    timestamp: int | None = None,
) -> MergeProbeReceipt:
    """Compile and sign a :class:`MergeProbeReceipt`.

    Args:
        probe: The measurement to seal.
        private_key_pem: The install's Ed25519 private key in PEM format.
            Source it from
            :func:`~bernstein.core.lineage.identity.load_or_create_signing_identity`
            rather than passing an empty string: an unsigned receipt anchors a
            hash nobody can attribute, which is most of the value gone.
        public_key_pem: The matching public key, embedded so a verifier
            holding only the receipt can check it.
        timestamp: Unix seconds; defaults to now. Never enters the signed body.

    Returns:
        The sealed receipt.
    """
    unsigned = receipt_from_probe(probe)
    stamped = int(timestamp if timestamp is not None else time.time())
    signature = sign_detached(unsigned.signing_input(), private_key_pem, kid=MERGE_PROBE_KID)
    return MergeProbeReceipt(
        **unsigned.to_canonical_dict(),
        timestamp=stamped,
        signer_public_key_pem=public_key_pem,
        signature=signature,
    )


def verify_probe_receipt(receipt: MergeProbeReceipt) -> bool:
    """Verify the detached signature against the receipt's embedded key.

    Returns False on any tamper: a mutated body changes the signing input, and
    a foreign-domain signature is over a different preimage. Never raises.
    """
    if not receipt.signature or not receipt.signer_public_key_pem:
        return False
    card = AgentCard(
        agent_id="install",
        kid=MERGE_PROBE_KID,
        public_key_pem=receipt.signer_public_key_pem,
    )
    return verify_detached(receipt.signing_input(), receipt.signature, card)


# ---------------------------------------------------------------------------
# Chain anchoring
# ---------------------------------------------------------------------------


def anchor_probe_receipt(chain: AuditChainStore, receipt: MergeProbeReceipt) -> AuditEvent:
    """Anchor a receipt's identity into the HMAC audit chain.

    Records the content hash plus the fields an operator reads when scanning
    the chain, so a ``merge.probe_receipt`` entry pins exactly this probe.
    """
    from bernstein.core.security.audit_chain import record_merge_probe

    return record_merge_probe(
        chain=chain,
        a_commit=receipt.a_commit,
        b_commit=receipt.b_commit,
        tree_id=receipt.tree_id,
        verdict=receipt.verdict,
        receipt_hash=receipt.receipt_hash(),
    )


# ---------------------------------------------------------------------------
# Offline re-derivation
# ---------------------------------------------------------------------------


def rederive_probe_receipt(receipt: MergeProbeReceipt, cwd: Path) -> ProbeVerification:
    """Re-run the recorded probe and compare it to what the receipt claims.

    Nothing recorded is trusted. The signature is checked first -- re-deriving
    a body that was never signed by anyone answers the wrong question -- then
    the probe is re-run against *cwd* and the tree id and conflicted-path
    digest must both reproduce.

    Config drift short-circuits before the tree comparison. Under a different
    merge configuration the recomputed tree is a different-but-correct answer
    to a different question, and reporting it as a mismatch would accuse an
    operator of forgery for editing ``.gitattributes``.

    Args:
        receipt: The receipt to check.
        cwd: A repository holding both recorded commits.

    Returns:
        The outcome. Never raises.
    """
    if not verify_probe_receipt(receipt):
        return ProbeVerification(
            status=VERIFY_SIGNATURE_INVALID,
            detail="detached signature does not verify against the embedded key",
        )

    probe = probe_integration(receipt.a_commit, receipt.b_commit, cwd)

    if probe.verdict is ProbeVerdict.UNAVAILABLE:
        return ProbeVerification(
            status=VERIFY_UNVERIFIABLE,
            detail=f"probe could not run here: {probe.reason}",
        )

    if probe.merge_config_digest != receipt.merge_config_digest:
        return ProbeVerification(
            status=VERIFY_CONFIG_DRIFT,
            detail=(
                f"merge configuration moved since the receipt was minted: "
                f"recorded {receipt.merge_config_digest}, found {probe.merge_config_digest}"
            ),
            recomputed_tree_id=probe.tree_id,
        )

    if probe.tree_id != receipt.tree_id:
        return ProbeVerification(
            status=VERIFY_TREE_MISMATCH,
            detail=f"recorded tree {receipt.tree_id}, re-derived {probe.tree_id}",
            recomputed_tree_id=probe.tree_id,
        )

    if probe.conflicted_paths_digest != receipt.conflicted_paths_digest:
        return ProbeVerification(
            status=VERIFY_PATHS_MISMATCH,
            detail=(
                f"recorded conflicted-path digest {receipt.conflicted_paths_digest}, "
                f"re-derived {probe.conflicted_paths_digest}"
            ),
            recomputed_tree_id=probe.tree_id,
        )

    return ProbeVerification(status=VERIFY_OK, recomputed_tree_id=probe.tree_id)


# ---------------------------------------------------------------------------
# On-disk persistence
# ---------------------------------------------------------------------------


def _safe_component(value: str) -> str:
    """Validate a path component for safe use in file paths."""
    if not value or "/" in value or "\\" in value or "\x00" in value or value in {".", ".."}:
        raise ValueError(f"unsafe path component: {value!r}")
    return value


def probe_receipt_dir(sdd_dir: Path) -> Path:
    """Return the directory probe receipts are written under."""
    return sdd_dir / "merge_probe" / "receipts"


def write_probe_receipt(sdd_dir: Path, receipt: MergeProbeReceipt) -> Path:
    """Persist a receipt as a content-addressed JSON record.

    The filename is the receipt's content hash, so re-writing an identical
    probe is idempotent and the file name is itself the anchor.
    """
    directory = probe_receipt_dir(sdd_dir)
    directory.mkdir(parents=True, exist_ok=True)
    digest = receipt.receipt_hash().split(":", 1)[-1]
    path = directory / f"{_safe_component(digest)}.json"
    path.write_text(
        json.dumps(receipt.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    return path


def read_probe_receipt(path: Path) -> MergeProbeReceipt | None:
    """Load a receipt from disk; ``None`` on a missing / malformed file."""
    if not path.is_file():
        return None
    try:
        return MergeProbeReceipt.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
