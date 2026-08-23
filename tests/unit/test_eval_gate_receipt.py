"""Tests for the signed verdict receipt behind statistical eval gating (#2520).

A gate verdict is a receipt that carries its statistical evidence and is
anchored to the lineage spine and mirrored into the HMAC audit chain. Offline
verification re-derives the verdict from the stored evidence, so a receipt
whose evidence does not entail its verdict is rejected even when its hashes are
internally consistent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.security.audit_chain import (
    EVENT_EVAL_GATE_VERDICT,
    AuditChainStore,
)
from bernstein.eval.gate_receipt import (
    build_verdict_receipt,
    read_verdict_receipt,
    recompute_receipt_hash,
    verdict_receipt_path,
    verify_verdict_receipt,
)
from bernstein.eval.significance import Verdict

_KEY = b"k" * 32


def _outcomes(base_passes: int, cand_passes: int, n: int) -> tuple[dict[str, bool], dict[str, bool]]:
    base = {f"t{i:03d}": (i < base_passes) for i in range(n)}
    cand = {f"t{i:03d}": (i < cand_passes) for i in range(n)}
    return base, cand


def _build(
    tmp_path: Path,
    base: dict[str, bool],
    cand: dict[str, bool],
    *,
    chain: AuditChainStore | None = None,
    candidate_config_id: str = "cfg-candidate",
):
    return build_verdict_receipt(
        baseline_outcomes=base,
        candidate_outcomes=cand,
        candidate_config_id=candidate_config_id,
        baseline_config_id="cfg-baseline",
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        timestamp=1_700_000_000,
        chain=chain,
    )


# ---------------------------------------------------------------------------
# Determinism (AC1)
# ---------------------------------------------------------------------------


def test_same_result_sets_any_order_produce_identical_receipt(tmp_path: Path) -> None:
    base, cand = _outcomes(base_passes=4, cand_passes=12, n=16)
    machine_a = _build(tmp_path / "a", base, cand)
    # Reverse ingestion order on a second, independent workdir.
    base_rev = dict(reversed(list(base.items())))
    cand_rev = dict(reversed(list(cand.items())))
    machine_b = _build(tmp_path / "b", base_rev, cand_rev)
    assert machine_a.receipt_hash == machine_b.receipt_hash
    assert machine_a.canonical_payload_without_anchor() == machine_b.canonical_payload_without_anchor()
    assert machine_a.verdict == Verdict.SIGNIFICANT_IMPROVEMENT


# ---------------------------------------------------------------------------
# Offline verification + tamper matrix (AC2)
# ---------------------------------------------------------------------------


def test_receipt_verifies_offline(tmp_path: Path) -> None:
    base, cand = _outcomes(base_passes=4, cand_passes=12, n=16)
    receipt = _build(tmp_path, base, cand)
    result = verify_verdict_receipt(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        receipt_hash=receipt.receipt_hash,
    )
    assert result.ok, result.reason


@pytest.mark.parametrize(
    ("mutate_path", "new_value"),
    [
        (("evidence", "n_candidate"), 99),
        (("evidence", "interval_low"), -0.9),
        (("evidence", "effect"), 0.99),
        (("evidence", "verdict"), "significant_regression"),
        (("baseline_result_set_hash",), "sha256:" + "0" * 64),
        (("candidate_result_set_hash",), "sha256:" + "0" * 64),
    ],
)
def test_tampering_any_field_breaks_verification(
    tmp_path: Path, mutate_path: tuple[str, ...], new_value: object
) -> None:
    base, cand = _outcomes(base_passes=4, cand_passes=12, n=16)
    receipt = _build(tmp_path, base, cand)
    path = verdict_receipt_path(tmp_path, receipt.receipt_hash)
    payload = json.loads(path.read_text(encoding="utf-8"))
    target = payload
    for key in mutate_path[:-1]:
        target = target[key]
    target[mutate_path[-1]] = new_value
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = verify_verdict_receipt(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        receipt_hash=receipt.receipt_hash,
    )
    assert not result.ok


def test_internally_consistent_forged_verdict_is_rejected(tmp_path: Path) -> None:
    # Flip the verdict AND recompute the receipt hash so the receipt is
    # internally consistent; re-derivation from the evidence must still reject.
    base, cand = _outcomes(base_passes=4, cand_passes=12, n=16)
    receipt = _build(tmp_path, base, cand)
    path = verdict_receipt_path(tmp_path, receipt.receipt_hash)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["evidence"]["verdict"] = "significant_regression"
    payload["receipt_hash"] = recompute_receipt_hash(payload)
    # Re-key the on-disk file to the forged hash so the reader finds it.
    forged_path = verdict_receipt_path(tmp_path, payload["receipt_hash"])
    forged_path.write_text(json.dumps(payload), encoding="utf-8")
    result = verify_verdict_receipt(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        receipt_hash=payload["receipt_hash"],
    )
    assert not result.ok
    assert "entail" in result.reason or "verdict" in result.reason


def test_internally_consistent_forged_effect_is_rejected(tmp_path: Path) -> None:
    base, cand = _outcomes(base_passes=4, cand_passes=12, n=16)
    receipt = _build(tmp_path, base, cand)
    path = verdict_receipt_path(tmp_path, receipt.receipt_hash)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["evidence"]["effect"] = 0.99
    payload["receipt_hash"] = recompute_receipt_hash(payload)
    forged_path = verdict_receipt_path(tmp_path, payload["receipt_hash"])
    forged_path.write_text(json.dumps(payload), encoding="utf-8")
    result = verify_verdict_receipt(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        receipt_hash=payload["receipt_hash"],
    )
    assert not result.ok


# ---------------------------------------------------------------------------
# Audit chain mirroring
# ---------------------------------------------------------------------------


def test_receipt_mirrors_into_audit_chain(tmp_path: Path) -> None:
    base, cand = _outcomes(base_passes=4, cand_passes=12, n=16)
    chain = AuditChainStore(tmp_path / "audit", key=_KEY)
    receipt = _build(tmp_path, base, cand, chain=chain)
    events = chain.query(event_type=EVENT_EVAL_GATE_VERDICT)
    assert len(events) == 1
    assert events[0].resource_id == receipt.receipt_hash
    assert events[0].details["verdict"] == Verdict.SIGNIFICANT_IMPROVEMENT.value
    ok, errors = chain.verify()
    assert ok, errors


def test_read_missing_receipt_returns_none(tmp_path: Path) -> None:
    assert read_verdict_receipt(tmp_path, "sha256:" + "a" * 64) is None


# ---------------------------------------------------------------------------
# Symlinked receipt store is refused, not followed
# ---------------------------------------------------------------------------


def test_a_symlinked_gate_receipt_store_is_refused_not_followed(tmp_path: Path) -> None:
    # A genuine receipt, sealed elsewhere, planted in an outside directory
    # that a symlinked gate store points at. realpath-vs-realpath containment
    # passes vacuously in that layout, so the store must refuse the symlink
    # itself.
    base, cand = _outcomes(base_passes=4, cand_passes=12, n=16)
    elsewhere = tmp_path / "elsewhere"
    receipt = _build(elsewhere, base, cand)
    receipt_json = verdict_receipt_path(elsewhere, receipt.receipt_hash).read_text(encoding="utf-8")

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / f"{receipt.receipt_hash}.json").write_text(receipt_json, encoding="utf-8")

    victim = tmp_path / "victim"
    (victim / ".sdd" / "eval").mkdir(parents=True)
    (victim / ".sdd" / "eval" / "gate").symlink_to(outside)

    assert read_verdict_receipt(victim, receipt.receipt_hash) is None
    with pytest.raises(ValueError, match="symlink"):
        verdict_receipt_path(victim, receipt.receipt_hash)
    result = verify_verdict_receipt(
        workdir=victim,
        lineage_root=victim / ".sdd" / "lineage",
        hmac_key=_KEY,
        receipt_hash=receipt.receipt_hash,
    )
    assert not result.ok
    assert "no verdict receipt" in result.reason


class _JunctionProbePath(Path):
    """POSIX stand-in for an NTFS junction on the store walk.

    ``is_junction()`` answers ``True`` for the marked component while
    ``is_symlink()`` keeps its real (``False``) answer, so
    ``is_filesystem_link`` takes its junction branch through the real
    component walk rather than having the whole check stubbed away.
    """

    _junction_component = "gate"

    def is_junction(self) -> bool:
        return self.name == self._junction_component


def test_a_junction_store_component_is_refused_like_a_symlink(tmp_path: Path) -> None:
    # Path.is_symlink() is False for NTFS junctions, so a symlink-only probe
    # is bypassed by a junctioned store component on Windows. A component
    # that is a filesystem link of any kind must be refused before the
    # realpath containment check, with no candidate returned.
    base, cand = _outcomes(base_passes=4, cand_passes=12, n=16)
    receipt = _build(tmp_path, base, cand)
    workdir = _JunctionProbePath(tmp_path)

    with pytest.raises(ValueError, match="symlink or junction"):
        verdict_receipt_path(workdir, receipt.receipt_hash)
    assert read_verdict_receipt(workdir, receipt.receipt_hash) is None


class _UnprobeableProbePath(Path):
    """Store walk stand-in whose link probe itself fails.

    ``is_symlink`` raises ``PermissionError`` for the marked component, so
    the fail-closed caller-side probe sees a genuine probe failure through
    the real walk rather than a stubbed helper.
    """

    _unprobeable_component = "gate"

    def is_symlink(self) -> bool:
        if self.name == self._unprobeable_component:
            raise PermissionError(13, "Permission denied")
        return super().is_symlink()


def test_an_unprobeable_store_component_is_refused_by_name(tmp_path: Path) -> None:
    # The shared best-effort helper answers False on a probe error; a store
    # walk that cannot prove a component is not a link must fail closed.
    base, cand = _outcomes(base_passes=4, cand_passes=12, n=16)
    receipt = _build(tmp_path, base, cand)
    workdir = _UnprobeableProbePath(tmp_path)

    with pytest.raises(ValueError, match="could not be probed for links"):
        verdict_receipt_path(workdir, receipt.receipt_hash)
    assert read_verdict_receipt(workdir, receipt.receipt_hash) is None


def test_a_symlink_planted_at_the_receipt_leaf_is_not_followed(tmp_path: Path) -> None:
    # A leaf symlink yields no parsed content: a link pointing outside the
    # store is refused by containment, and the no-follow leaf open rejects a
    # symlink swapped in after path validation (the TOCTOU window) with
    # ELOOP -- pinned against the real filesystem, no mocking.
    import errno as _errno

    from bernstein.eval.gate_receipt import _read_leaf_text

    base, cand = _outcomes(base_passes=4, cand_passes=12, n=16)
    receipt = _build(tmp_path, base, cand)
    leaf = verdict_receipt_path(tmp_path, receipt.receipt_hash)
    outside = tmp_path / "outside"
    outside.mkdir()
    stolen = outside / "planted.json"
    stolen.write_text(leaf.read_text(encoding="utf-8"), encoding="utf-8")
    leaf.unlink()
    leaf.symlink_to(stolen)

    assert read_verdict_receipt(tmp_path, receipt.receipt_hash) is None
    with pytest.raises(OSError) as excinfo:
        _read_leaf_text(leaf)
    assert excinfo.value.errno == _errno.ELOOP


# ---------------------------------------------------------------------------
# Three-valued verdicts: round-trip, re-derivation, backward compat (AC3)
# ---------------------------------------------------------------------------

_VERDICT_STATES = [
    (4, 12, 16, Verdict.SIGNIFICANT_IMPROVEMENT, "significant_improvement"),
    (12, 4, 16, Verdict.SIGNIFICANT_REGRESSION, "significant_regression"),
    (0, 8, 8, Verdict.INSUFFICIENT_EVIDENCE, "below_minimum_n"),
]


@pytest.mark.parametrize(
    ("base_passes", "cand_passes", "n", "expected_verdict", "expected_reason"),
    _VERDICT_STATES,
)
def test_round_trip_preserves_verdict_and_reason_byte_exactly(
    tmp_path: Path,
    base_passes: int,
    cand_passes: int,
    n: int,
    expected_verdict: Verdict,
    expected_reason: str,
) -> None:
    base, cand = _outcomes(base_passes, cand_passes, n)
    receipt = _build(tmp_path, base, cand)
    parsed = read_verdict_receipt(tmp_path, receipt.receipt_hash)
    assert parsed is not None
    assert parsed.verdict == expected_verdict
    assert parsed.evidence.reason == expected_reason
    # Byte-exact: the parsed receipt re-serializes to the sealed canonical bytes.
    assert parsed.canonical_payload_without_anchor() == receipt.canonical_payload_without_anchor()


@pytest.mark.parametrize(
    ("base_passes", "cand_passes", "n", "expected_verdict", "expected_reason"),
    _VERDICT_STATES,
)
def test_verifier_rederives_same_verdict_from_receipt_alone(
    tmp_path: Path,
    base_passes: int,
    cand_passes: int,
    n: int,
    expected_verdict: Verdict,
    expected_reason: str,
) -> None:
    base, cand = _outcomes(base_passes, cand_passes, n)
    receipt = _build(tmp_path, base, cand)
    result = verify_verdict_receipt(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        receipt_hash=receipt.receipt_hash,
    )
    assert result.ok, result.reason
    assert result.receipt is not None
    assert result.receipt.verdict == expected_verdict
    assert result.receipt.evidence.reason == expected_reason


def test_schema_v1_receipt_without_reason_parses_as_binary_era(tmp_path: Path) -> None:
    # A schema_version=1 receipt sealed before three-valued verdicts carried no
    # `reason` in its evidence. It must still parse (reason defaulting to empty)
    # rather than being rejected as malformed.
    base, cand = _outcomes(base_passes=4, cand_passes=12, n=16)
    receipt = _build(tmp_path, base, cand)
    path = verdict_receipt_path(tmp_path, receipt.receipt_hash)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    del payload["evidence"]["reason"]
    payload["receipt_hash"] = recompute_receipt_hash(payload)
    binary_era_path = verdict_receipt_path(tmp_path, payload["receipt_hash"])
    binary_era_path.write_text(json.dumps(payload), encoding="utf-8")

    parsed = read_verdict_receipt(tmp_path, payload["receipt_hash"])
    assert parsed is not None
    assert parsed.schema_version == 1
    assert parsed.verdict == Verdict.SIGNIFICANT_IMPROVEMENT
    assert parsed.evidence.reason == ""


@pytest.mark.parametrize(
    ("base_passes", "cand_passes", "n"),
    [(4, 12, 16), (12, 4, 16), (0, 8, 8)],
)
def test_determinism_across_verdict_states(tmp_path: Path, base_passes: int, cand_passes: int, n: int) -> None:
    base, cand = _outcomes(base_passes, cand_passes, n)
    machine_a = _build(tmp_path / "a", base, cand)
    base_rev = dict(reversed(list(base.items())))
    cand_rev = dict(reversed(list(cand.items())))
    machine_b = _build(tmp_path / "b", base_rev, cand_rev)
    assert machine_a.receipt_hash == machine_b.receipt_hash
    assert machine_a.canonical_payload_without_anchor() == machine_b.canonical_payload_without_anchor()
