"""Declared-vs-produced output diff, sealed and verifiable (issue #2559).

``Task.declared_outputs`` states what a task intends to leave behind. At
completion the gate compares that intent against what was actually produced and
seals the three-way result into the evidence bundle's *signed binding*.

Two facts the chain could not state before, and which these tests pin:

* a task that declared an output and did not produce it leaves an
  artifact-keyed record, so "attempted and failed" is distinguishable from
  "nothing was ever scheduled";
* a write nobody declared becomes a signed finding, verifiable offline, rather
  than something a reviewer has to notice in a diff.
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING

import pytest

from bernstein.core.evidence.bundle import (
    EvidenceBundle,
    bundle_path,
    read_evidence_bundle,
    verify_evidence_bundle,
)
from bernstein.core.evidence.completion_gate import seal_evidence_on_completion
from bernstein.core.evidence.output_diff import OutputDiff, compute_output_diff
from bernstein.core.lineage.artifact_uri import ArtifactURIError
from bernstein.core.tasks.models import Task

if TYPE_CHECKING:
    from pathlib import Path


def _task(task_id: str, *, declared: list[str] | None = None, producers: list[dict[str, object]] | None = None) -> Task:
    return Task(
        id=task_id,
        title="do the thing",
        description="body",
        role="backend",
        evidence_producers=producers or [],
        declared_outputs=declared or [],
    )


def _isolate_audit_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))


# ---------------------------------------------------------------------------
# The pure three-way diff
# ---------------------------------------------------------------------------


def test_three_way_diff() -> None:
    diff = compute_output_diff(
        declared=["dist/*.whl", "pkg://pypi/bernstein/3.9.0", "doc://example.test/lineage"],
        produced=["dist/bernstein-3.9.0.whl", "pkg://pypi/bernstein/3.9.0", "src/sneaky.py"],
    )
    assert diff.declared_and_produced == ("dist/bernstein-3.9.0.whl", "pkg://pypi/bernstein/3.9.0")
    assert diff.declared_but_missing == ("doc://example.test/lineage",)
    assert diff.produced_but_undeclared == ("src/sneaky.py",)
    assert diff.has_findings
    assert not diff.is_empty


def test_a_declared_output_that_was_never_produced_is_named() -> None:
    """ "Attempted and failed" is a fact, not an absence.

    Without this bucket, a task that dies before producing its package leaves
    exactly what a task that was never scheduled leaves: nothing.
    """
    diff = compute_output_diff(declared=["pkg://pypi/bernstein/3.9.0"], produced=[])
    assert diff.declared_but_missing == ("pkg://pypi/bernstein/3.9.0",)
    assert diff.declared_and_produced == ()
    assert diff.has_findings


def test_no_declaration_and_no_production_is_an_empty_diff() -> None:
    diff = compute_output_diff(declared=[], produced=[])
    assert diff.is_empty
    assert not diff.has_findings


def test_diff_is_order_independent_and_deduplicating() -> None:
    """Determinism: the diff is a function of the two *sets*, not of order."""
    a = compute_output_diff(
        declared=["dist/*.whl", "pkg://pypi/bernstein/3.9.0"],
        produced=["src/x.py", "dist/a.whl", "dist/a.whl"],
    )
    b = compute_output_diff(
        declared=["pkg://pypi/bernstein/3.9.0", "dist/*.whl", "dist/*.whl"],
        produced=["dist/a.whl", "src/x.py"],
    )
    assert a == b
    assert json.dumps(a.to_dict(), sort_keys=True) == json.dumps(b.to_dict(), sort_keys=True)


def test_diff_canonicalises_both_sides() -> None:
    diff = compute_output_diff(
        declared=["PKG://PyPI/bernstein/3.9.0"],
        produced=["pkg://pypi/bernstein/3.9.0"],
    )
    assert diff.declared_and_produced == ("pkg://pypi/bernstein/3.9.0",)
    assert diff.declared_but_missing == ()


def test_diff_refuses_an_invalid_key() -> None:
    with pytest.raises(ArtifactURIError):
        compute_output_diff(declared=["ftp://evil.test/x"], produced=[])


def test_diff_round_trips_through_its_dict_form() -> None:
    diff = compute_output_diff(declared=["dist/*.whl"], produced=["dist/a.whl", "src/x.py"])
    assert OutputDiff.from_dict(diff.to_dict()) == diff


# ---------------------------------------------------------------------------
# Task.declared_outputs
# ---------------------------------------------------------------------------


def test_declared_outputs_are_canonicalised_sorted_and_deduplicated() -> None:
    task = _task(
        "T-1",
        declared=[
            "PKG://PyPI/bernstein/3.9.0",
            "dist/*.whl",
            "pkg://pypi/bernstein/3.9.0",
            "repo://src/a.py",
        ],
    )
    reloaded = Task.from_dict(task.to_dict())
    assert reloaded.declared_outputs == [
        "dist/*.whl",
        "pkg://pypi/bernstein/3.9.0",
        "src/a.py",
    ]


def test_declared_outputs_default_to_empty() -> None:
    task = Task(id="T-2", title="t", description="d", role="backend")
    assert task.declared_outputs == []
    assert Task.from_dict(task.to_dict()).declared_outputs == []


@pytest.mark.parametrize(
    "declared",
    [
        ["ftp://evil.test/payload"],
        ["../../etc/passwd"],
        ["/etc/passwd"],
        ["pkg://pypi/../secrets/1.0"],
    ],
)
def test_a_malformed_declaration_fails_loudly(declared: list[str]) -> None:
    """A declaration that quietly vanished would take its finding with it."""
    with pytest.raises(ArtifactURIError):
        Task.from_dict({"id": "T-3", "title": "t", "description": "d", "role": "backend", "declared_outputs": declared})


@pytest.mark.parametrize("declared", ["dist/*.whl", {"a": 1}, [1, 2]])
def test_declared_outputs_shape_is_validated(declared: object) -> None:
    with pytest.raises(TypeError):
        Task.from_dict({"id": "T-4", "title": "t", "description": "d", "role": "backend", "declared_outputs": declared})


# ---------------------------------------------------------------------------
# Sealing into the signed bundle
# ---------------------------------------------------------------------------

_PRODUCER = [{"name": "tests", "kind": "test", "command": [sys.executable, "-c", "print('ok')"], "required": True}]


def test_diff_is_sealed_and_verifies_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_audit_key(tmp_path, monkeypatch)
    task = _task("T-seal", declared=["dist/*.whl", "pkg://pypi/bernstein/3.9.0"], producers=_PRODUCER)

    bundle = seal_evidence_on_completion(
        tmp_path,
        task,
        timestamp=1234,
        produced_outputs=["dist/bernstein-3.9.0.whl", "src/sneaky.py"],
    )
    assert bundle is not None
    assert bundle.output_diff is not None
    assert bundle.output_diff.produced_but_undeclared == ("src/sneaky.py",)
    assert bundle.output_diff.declared_but_missing == ("pkg://pypi/bernstein/3.9.0",)

    reloaded = read_evidence_bundle(tmp_path, "T-seal")
    assert reloaded is not None
    assert reloaded.output_diff == bundle.output_diff

    result = verify_evidence_bundle(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_load_key(),
        task_id="T-seal",
    )
    assert result.ok, result.reason


def _load_key() -> bytes:
    from bernstein.core.security.audit import load_or_create_audit_key

    return load_or_create_audit_key()


def test_tampering_with_the_finding_breaks_verification(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The finding is inside the binding, so it cannot be quietly removed.

    An undeclared write is only worth recording if the record survives the
    party with the strongest motive to erase it.
    """
    _isolate_audit_key(tmp_path, monkeypatch)
    task = _task("T-tamper", declared=["dist/*.whl"], producers=_PRODUCER)
    seal_evidence_on_completion(
        tmp_path,
        task,
        timestamp=1234,
        produced_outputs=["dist/a.whl", "src/sneaky.py"],
    )

    path = bundle_path(tmp_path, "T-tamper")
    row = json.loads(path.read_text(encoding="utf-8"))
    assert row["output_diff"]["produced_but_undeclared"] == ["src/sneaky.py"]
    row["output_diff"]["produced_but_undeclared"] = []
    path.write_text(json.dumps(row, ensure_ascii=False, separators=(",", ":"), sort_keys=True), encoding="utf-8")

    result = verify_evidence_bundle(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_load_key(),
        task_id="T-tamper",
    )
    assert not result.ok


def test_dropping_the_diff_entirely_also_breaks_verification(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_audit_key(tmp_path, monkeypatch)
    task = _task("T-drop", declared=["dist/*.whl"], producers=_PRODUCER)
    seal_evidence_on_completion(tmp_path, task, timestamp=1234, produced_outputs=["src/sneaky.py"])

    path = bundle_path(tmp_path, "T-drop")
    row = json.loads(path.read_text(encoding="utf-8"))
    del row["output_diff"]
    path.write_text(json.dumps(row, ensure_ascii=False, separators=(",", ":"), sort_keys=True), encoding="utf-8")

    result = verify_evidence_bundle(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_load_key(),
        task_id="T-drop",
    )
    assert not result.ok


# ---------------------------------------------------------------------------
# Backward compatibility of the bundle wire form
# ---------------------------------------------------------------------------


def test_a_bundle_without_a_diff_canonicalises_exactly_as_before() -> None:
    """Bundles sealed before #2559 keep their signature and their anchor.

    The diff is dropped from the binding when absent or empty, the same rule
    ``trust_class`` follows on a lineage entry, so a pre-feature bundle's bytes
    are untouched.
    """
    base = EvidenceBundle(task_id="T-old", items=(), gate_passed=True, timestamp=7)
    with_empty = EvidenceBundle(
        task_id="T-old",
        items=(),
        gate_passed=True,
        timestamp=7,
        output_diff=OutputDiff(),
    )
    assert base.to_canonical_bytes() == with_empty.to_canonical_bytes()
    assert base.bundle_hash() == with_empty.bundle_hash()
    assert b"output_diff" not in base.to_canonical_bytes()

    populated = EvidenceBundle(
        task_id="T-old",
        items=(),
        gate_passed=True,
        timestamp=7,
        output_diff=OutputDiff(produced_but_undeclared=("src/x.py",)),
    )
    assert populated.bundle_hash() != base.bundle_hash()


def test_a_pre_feature_bundle_reads_back_with_no_diff() -> None:
    raw = json.dumps(
        {
            "v": 1,
            "task_id": "T-old",
            "items": [],
            "gate_passed": True,
            "timestamp": 7,
            "signer_public_key_pem": "",
            "signature": "",
            "journal_entry_hash": "",
        }
    ).encode("utf-8")
    assert EvidenceBundle.from_bytes(raw).output_diff is None


# ---------------------------------------------------------------------------
# Failure-domain isolation
# ---------------------------------------------------------------------------


def test_no_observation_seals_no_diff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Not observing production is not the same as observing no production.

    Sealing an all-missing diff from an absent observation would manufacture
    findings out of ignorance, so the diff is skipped entirely.
    """
    _isolate_audit_key(tmp_path, monkeypatch)
    task = _task("T-unobserved", declared=["dist/*.whl"], producers=_PRODUCER)
    bundle = seal_evidence_on_completion(tmp_path, task, timestamp=1234)
    assert bundle is not None
    assert bundle.output_diff is None


def test_a_task_declaring_nothing_is_still_a_zero_touch_no_op(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_audit_key(tmp_path, monkeypatch)
    task = _task("T-none")
    assert seal_evidence_on_completion(tmp_path, task, timestamp=1234, produced_outputs=["src/x.py"]) is None
    assert not (tmp_path / ".sdd" / "evidence").exists()


def test_a_broken_diff_never_fails_a_completing_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fault injection: the diff is fail-open with respect to completion.

    A task that has already completed must not be failed retroactively by the
    bookkeeping that describes it.
    """
    _isolate_audit_key(tmp_path, monkeypatch)

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("projection exploded")

    monkeypatch.setattr("bernstein.core.evidence.completion_gate.compute_output_diff", _boom)
    task = _task("T-faulty", declared=["dist/*.whl"], producers=_PRODUCER)
    assert seal_evidence_on_completion(tmp_path, task, timestamp=1234, produced_outputs=["src/x.py"]) is None


def test_an_undeclarable_produced_key_never_fails_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_audit_key(tmp_path, monkeypatch)
    task = _task("T-badkey", declared=["dist/*.whl"], producers=_PRODUCER)
    assert seal_evidence_on_completion(tmp_path, task, timestamp=1234, produced_outputs=["ftp://evil.test/x"]) is None
