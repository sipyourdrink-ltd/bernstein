"""Unit tests for ``core.replay.diagnose`` and its signal adapters (#2928).

Hermetic throughout: every journal is a synthetic ``EventJournal`` under
``tmp_path``, every signal is resolved from synthetic on-disk records, and
no network or live provider is touched.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import TYPE_CHECKING

import pytest

from bernstein.core.lineage.entry import LineageEntry, entry_hash
from bernstein.core.replay.diagnose import (
    DiagnoseError,
    SignalNotLocatedError,
    SignalPredicate,
    diagnose_run,
)
from bernstein.core.replay.diagnose_signals import (
    artefact_signal,
    gate_signal,
    incident_signal,
    predicate_from_params,
    replay_signal,
    resolve_signal,
)
from bernstein.core.replay.diff import (
    REASON_CODE_BAD_INPUT_CONTENT_HASH,
    REASON_CODE_CHAIN_BREAK,
    REASON_CODE_FIRST_FAILING_TOOL_RESULT,
    REASON_CODE_LENGTH_MISMATCH,
    REASON_CODE_NONE,
    REASON_CODE_PROVIDER_STATE_MUTATION,
    REASON_CODE_RESPONSE_MISMATCH,
)
from bernstein.core.replay.journal import EventJournal, load_events
from bernstein.core.replay.provider_state import PROVIDER_STATE_MUTATION_EVENT

if TYPE_CHECKING:
    from pathlib import Path

BAD_HASH = hashlib.sha256(b"the-offending-content").hexdigest()


def _content_predicate(*needles: str) -> SignalPredicate:
    return SignalPredicate(
        predicate_id="test/v1",
        params={"kind": "incident", "needles": sorted(needles)},
        default_reason_code=REASON_CODE_FIRST_FAILING_TOOL_RESULT,
        needles=tuple(sorted(needles)),
    )


def _seed_journal(sdd: Path, run_id: str, *, bad_step: int | None, steps: int = 5) -> Path:
    """Build a journal of *steps* ticks; *bad_step* records the bad hash."""
    journal = EventJournal(run_id, sdd)
    for i in range(steps):
        if i == bad_step:
            journal.record("tool_result", step=i, content_hash=f"sha256:{BAD_HASH}")
        else:
            journal.record("tick", step=i)
    return journal.path


def _tamper_payload(journal_path: Path, index: int) -> None:
    """Mutate the payload of row *index* without recomputing its hashes."""
    lines = journal_path.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[index])
    row["step"] = "tampered"
    lines[index] = json.dumps(row, default=str)
    journal_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# locator core
# ---------------------------------------------------------------------------


def test_diagnosis_names_first_divergent_step_index(tmp_path: Path) -> None:
    """The culprit is the exact step that introduced the bad content hash."""
    sdd = tmp_path / ".sdd"
    path = _seed_journal(sdd, "run-1", bad_step=3)

    result = diagnose_run(path, _content_predicate(BAD_HASH), run_id="run-1")

    assert result.located is True
    assert result.culprit_index == 3
    events = load_events(path)
    assert result.culprit_step_hash == events[3]["event_hash"]
    assert result.event_count == 5
    assert result.reason_code == REASON_CODE_FIRST_FAILING_TOOL_RESULT


def test_culprit_is_the_first_of_repeated_appearances(tmp_path: Path) -> None:
    """When the bad hash recurs, the minimal (first) index is named."""
    sdd = tmp_path / ".sdd"
    journal = EventJournal("run-rep", sdd)
    journal.record("tick", step=0)
    journal.record("tool_result", step=1, content_hash=f"sha256:{BAD_HASH}")
    journal.record("tool_result", step=2, content_hash=f"sha256:{BAD_HASH}")

    result = diagnose_run(journal.path, _content_predicate(BAD_HASH), run_id="run-rep")

    assert result.culprit_index == 1


def test_untampered_run_diagnoses_clean(tmp_path: Path) -> None:
    """The replay signal over an intact chain locates nothing (clean)."""
    sdd = tmp_path / ".sdd"
    path = _seed_journal(sdd, "run-clean", bad_step=None)

    result = diagnose_run(path, replay_signal(), run_id="run-clean")

    assert result.located is False
    assert result.culprit_index is None
    assert result.reason_code == REASON_CODE_NONE
    assert "chain intact" in result.reason


def test_replay_signal_names_chain_break_index(tmp_path: Path) -> None:
    """A payload mutated at step j surfaces as a chain break at exactly j."""
    sdd = tmp_path / ".sdd"
    path = _seed_journal(sdd, "run-tamper", bad_step=None)
    _tamper_payload(path, 2)

    result = diagnose_run(path, replay_signal(), run_id="run-tamper")

    assert result.located is True
    assert result.culprit_index == 2
    assert result.reason_code == REASON_CODE_CHAIN_BREAK


def test_broken_chain_fails_closed_for_content_signals(tmp_path: Path) -> None:
    """Content predicates refuse an unverified record instead of scanning it."""
    sdd = tmp_path / ".sdd"
    path = _seed_journal(sdd, "run-broken", bad_step=3)
    _tamper_payload(path, 1)

    with pytest.raises(DiagnoseError, match="chain verification"):
        diagnose_run(path, _content_predicate(BAD_HASH), run_id="run-broken")


def test_missing_journal_fails_closed(tmp_path: Path) -> None:
    """No journal file: refuse with the no-signed-record message."""
    with pytest.raises(DiagnoseError, match="no signed per-step record"):
        diagnose_run(tmp_path / "absent.jsonl", replay_signal(), run_id="run-x")


def test_empty_journal_fails_closed(tmp_path: Path) -> None:
    """A blanked (zero-event) journal is refused, not reported clean."""
    empty = tmp_path / "journal.jsonl"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(DiagnoseError, match="no signed per-step record"):
        diagnose_run(empty, replay_signal(), run_id="run-x")


def test_malformed_tail_line_fails_closed(tmp_path: Path) -> None:
    """A torn/garbage tail line must refuse, never a receipt over a filtered
    sequence: the tolerant reader would drop it and the surviving prefix
    would chain-verify clean (regression for bot-ack: 3705961185)."""
    sdd = tmp_path / ".sdd"
    path = _seed_journal(sdd, "run-torn", bad_step=None)
    with path.open("a", encoding="utf-8") as f:
        f.write("{this is not json\n")

    with pytest.raises(DiagnoseError, match="unparsable line at physical line 5"):
        diagnose_run(path, replay_signal(), run_id="run-torn")


def test_malformed_middle_line_fails_closed_for_every_signal_mode(tmp_path: Path) -> None:
    """A malformed middle line refuses both chain and content predicates
    before any index is computed, so no reported culprit can ever count
    parsed rows instead of physical journal lines."""
    sdd = tmp_path / ".sdd"
    path = _seed_journal(sdd, "run-midtorn", bad_step=3)
    lines = path.read_text(encoding="utf-8").splitlines()
    lines.insert(2, "garbage that does not decode")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(DiagnoseError, match="unparsable line at physical line 2"):
        diagnose_run(path, replay_signal(), run_id="run-midtorn")
    with pytest.raises(DiagnoseError, match="unparsable line at physical line 2"):
        diagnose_run(path, _content_predicate(BAD_HASH), run_id="run-midtorn")


def test_non_object_json_row_fails_closed(tmp_path: Path) -> None:
    """A line that decodes to a JSON scalar is refused like garbage."""
    sdd = tmp_path / ".sdd"
    path = _seed_journal(sdd, "run-scalar", bad_step=None)
    with path.open("a", encoding="utf-8") as f:
        f.write("42\n")

    with pytest.raises(DiagnoseError, match="non-object row at physical line 5"):
        diagnose_run(path, replay_signal(), run_id="run-scalar")


def test_unlocatable_signal_refuses_instead_of_guessing(tmp_path: Path) -> None:
    """A fingerprint absent from every step raises SignalNotLocatedError."""
    sdd = tmp_path / ".sdd"
    path = _seed_journal(sdd, "run-nf", bad_step=None)

    with pytest.raises(SignalNotLocatedError, match="does not resolve to any recorded step"):
        diagnose_run(path, _content_predicate(BAD_HASH), run_id="run-nf")


def test_provider_state_mutation_step_is_attributed(tmp_path: Path) -> None:
    """A mutation-event culprit is attributed with the shared mutation code."""
    sdd = tmp_path / ".sdd"
    journal = EventJournal("run-mut", sdd)
    journal.record("tick", step=0)
    journal.record(
        PROVIDER_STATE_MUTATION_EVENT,
        mutation_kind="context_edit",
        content_hash=f"sha256:{BAD_HASH}",
    )

    result = diagnose_run(journal.path, _content_predicate(BAD_HASH), run_id="run-mut")

    assert result.culprit_index == 1
    assert result.reason_code == REASON_CODE_PROVIDER_STATE_MUTATION


# ---------------------------------------------------------------------------
# shared reason-code vocabulary
# ---------------------------------------------------------------------------


def test_reason_code_vocabulary_stays_backward_compatible() -> None:
    """Existing two-run diff codes keep their exact values; additions are new.

    Diagnosis and two-run diff share one machine-readable set, so the
    pre-existing constants must never change value and the diagnosis codes
    must not collide with them.
    """
    assert REASON_CODE_NONE == ""
    assert REASON_CODE_RESPONSE_MISMATCH == "response_mismatch"
    assert REASON_CODE_LENGTH_MISMATCH == "length_mismatch"
    assert REASON_CODE_PROVIDER_STATE_MUTATION == "provider_state_mutation"
    additions = {
        REASON_CODE_CHAIN_BREAK,
        REASON_CODE_FIRST_FAILING_TOOL_RESULT,
        REASON_CODE_BAD_INPUT_CONTENT_HASH,
    }
    existing = {
        REASON_CODE_NONE,
        REASON_CODE_RESPONSE_MISMATCH,
        REASON_CODE_LENGTH_MISMATCH,
        REASON_CODE_PROVIDER_STATE_MUTATION,
    }
    assert len(additions) == 3
    assert not (additions & existing)


# ---------------------------------------------------------------------------
# gate signal
# ---------------------------------------------------------------------------


def _write_gate_receipt(gate_dir: Path, *, timestamp: int) -> str:
    """Seal a synthetic rejecting VerdictReceipt to disk; returns its hash."""
    from bernstein.eval.gate_receipt import VerdictReceipt, recompute_receipt_hash
    from bernstein.eval.significance import PairedTable, classify, result_set_hash, suite_content_hash

    tasks = {f"t{i}": True for i in range(40)}
    baseline = dict(tasks)
    candidate = {k: False for k in tasks}
    table = PairedTable.from_outcomes(baseline, candidate)
    evidence = classify(table)
    assert evidence.verdict.value == "significant_regression"

    unsealed = VerdictReceipt(
        schema_version=1,
        suite_content_hash=suite_content_hash(list(tasks)),
        baseline_result_set_hash=result_set_hash(baseline),
        candidate_result_set_hash=result_set_hash(candidate),
        candidate_config_id="cand-1",
        baseline_config_id="base-1",
        evidence=evidence,
        timestamp=timestamp,
        receipt_hash="",
    )
    receipt_hash = recompute_receipt_hash(unsealed.to_dict())
    sealed = VerdictReceipt(
        schema_version=1,
        suite_content_hash=unsealed.suite_content_hash,
        baseline_result_set_hash=unsealed.baseline_result_set_hash,
        candidate_result_set_hash=unsealed.candidate_result_set_hash,
        candidate_config_id="cand-1",
        baseline_config_id="base-1",
        evidence=evidence,
        timestamp=timestamp,
        receipt_hash=receipt_hash,
    )
    gate_dir.mkdir(parents=True, exist_ok=True)
    (gate_dir / f"{receipt_hash.replace(':', '_')}.json").write_text(
        json.dumps(sealed.to_dict(), sort_keys=True), encoding="utf-8"
    )
    return receipt_hash


def test_gate_signal_is_pure_function_of_on_disk_records(tmp_path: Path) -> None:
    """Two independent resolutions of the same store agree byte-for-byte."""
    gate_dir = tmp_path / ".sdd" / "eval" / "gate"
    receipt_hash = _write_gate_receipt(gate_dir, timestamp=100)

    first = gate_signal(None, gate_dir=gate_dir)
    second = gate_signal(None, gate_dir=gate_dir)

    assert first.params == second.params
    assert first.predicate_hash() == second.predicate_hash()
    assert first.params["receipt_hash"] == receipt_hash
    assert first.default_reason_code == REASON_CODE_BAD_INPUT_CONTENT_HASH
    assert first.needles  # the receipt's content hashes, bare-hex


def test_gate_signal_locates_step_recording_the_rejected_hash(tmp_path: Path) -> None:
    """The culprit is where the rejected check's content hash first appears."""
    sdd = tmp_path / ".sdd"
    gate_dir = sdd / "eval" / "gate"
    _write_gate_receipt(gate_dir, timestamp=100)
    predicate = gate_signal(None, gate_dir=gate_dir)
    suite_hash_hex = next(iter(predicate.needles))

    journal = EventJournal("run-gate", sdd)
    journal.record("tick", step=0)
    journal.record("eval_input", suite_content_hash=f"sha256:{suite_hash_hex}")
    journal.record("tick", step=2)

    result = diagnose_run(journal.path, predicate, run_id="run-gate")

    assert result.culprit_index == 1
    assert result.reason_code == REASON_CODE_BAD_INPUT_CONTENT_HASH


def test_gate_signal_fails_closed_when_no_rejecting_receipt(tmp_path: Path) -> None:
    with pytest.raises(DiagnoseError, match="no rejecting verdict receipt"):
        gate_signal(None, gate_dir=tmp_path / "gate")


# ---------------------------------------------------------------------------
# artefact signal
# ---------------------------------------------------------------------------


def _lineage_entry(
    path: str,
    *,
    kind: str,
    content: bytes,
    parents: list[str],
    ts_ns: int,
    trust_class: str | None = None,
) -> LineageEntry:
    return LineageEntry(
        v=1,
        artefact_path=path,
        artefact_kind=kind,
        content_hash="sha256:" + hashlib.sha256(content).hexdigest(),
        parent_hashes=parents,
        agent_id="agent-1",
        agent_card_kid="kid-1",
        tool_call_id="tc-1",
        span_id="span-1",
        ts_ns=ts_ns,
        operator_hmac="",
        trust_class=trust_class,
    )


def test_artefact_signal_locates_first_appearance_of_tainted_hash(tmp_path: Path) -> None:
    """Lineage walks back from the tip; the culprit step first records the
    tainted record's content hash, and the parent chain rides as evidence."""
    sdd = tmp_path / ".sdd"
    tainted = _lineage_entry(
        "provenance/web.fetch/aaaa",
        kind="tool-result",
        content=b"outsider-bytes",
        parents=[],
        ts_ns=1,
        trust_class="third_party",
    )
    tip = _lineage_entry(
        "out.txt",
        kind="file",
        content=b"derived-bytes",
        parents=[entry_hash(tainted)],
        ts_ns=2,
    )
    log_path = sdd / "lineage" / "log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("\n".join(json.dumps(asdict(e)) for e in (tainted, tip)) + "\n", encoding="utf-8")

    predicate = artefact_signal("out.txt", lineage_log=log_path)
    tainted_hex = tainted.content_hash.split(":", 1)[-1]
    assert predicate.needles == (tainted_hex,)
    assert predicate.lineage_path == (entry_hash(tainted), entry_hash(tip))

    journal = EventJournal("run-art", sdd)
    journal.record("tick", step=0)
    journal.record("tick", step=1)
    journal.record("tool_result", content_hash=tainted.content_hash)
    journal.record("tick", step=3)

    result = diagnose_run(journal.path, predicate, run_id="run-art")

    assert result.culprit_index == 2
    assert result.reason_code == REASON_CODE_BAD_INPUT_CONTENT_HASH
    assert result.lineage_path == (entry_hash(tainted), entry_hash(tip))


def test_artefact_signal_refuses_untainted_artefact(tmp_path: Path) -> None:
    clean = _lineage_entry("ok.txt", kind="file", content=b"fine", parents=[], ts_ns=1, trust_class="operator")
    log_path = tmp_path / "log.jsonl"
    log_path.write_text(json.dumps(asdict(clean)) + "\n", encoding="utf-8")

    with pytest.raises(DiagnoseError, match="not tainted"):
        artefact_signal("ok.txt", lineage_log=log_path)


def test_artefact_signal_fails_closed_without_lineage(tmp_path: Path) -> None:
    with pytest.raises(DiagnoseError, match="no lineage entries"):
        artefact_signal("out.txt", lineage_log=tmp_path / "missing.jsonl")


# ---------------------------------------------------------------------------
# incident signal
# ---------------------------------------------------------------------------


def test_incident_signal_matches_exact_recorded_failure_text(tmp_path: Path) -> None:
    cases = tmp_path / "cases"
    cases.mkdir()
    prompt = (
        "Reproduce and resolve the following terminal failure (role=qa).\n"
        "Task: flaky pipeline\n"
        "Failure reason: crash\n"
        "Last error (trimmed):\n"
        "ValueError: boom in step seven\n"
    )
    (cases / "inc-abc123.yaml").write_text(json.dumps({"id": "inc-abc123", "prompt": prompt}), encoding="utf-8")

    predicate = incident_signal("inc-abc123", cases_dir=cases)
    assert predicate.needles == ("ValueError: boom in step seven",)

    sdd = tmp_path / ".sdd"
    journal = EventJournal("run-inc", sdd)
    journal.record("tick", step=0)
    journal.record("tool_result", stderr="ValueError: boom in step seven")

    result = diagnose_run(journal.path, predicate, run_id="run-inc")

    assert result.culprit_index == 1
    assert result.reason_code == REASON_CODE_FIRST_FAILING_TOOL_RESULT


def test_incident_signal_fails_closed_on_missing_case(tmp_path: Path) -> None:
    with pytest.raises(DiagnoseError, match="no incident eval case"):
        incident_signal("inc-none", cases_dir=tmp_path)


def test_incident_signal_refuses_case_without_fingerprint(tmp_path: Path) -> None:
    (tmp_path / "inc-bare.yaml").write_text(
        json.dumps({"id": "inc-bare", "prompt": "Reproduce the failure.\n"}), encoding="utf-8"
    )
    with pytest.raises(DiagnoseError, match="no matchable failure fingerprint"):
        incident_signal("inc-bare", cases_dir=tmp_path)


# ---------------------------------------------------------------------------
# spec parsing + offline predicate round-trip
# ---------------------------------------------------------------------------


def test_resolve_signal_rejects_unknown_kind(tmp_path: Path) -> None:
    with pytest.raises(DiagnoseError, match="unknown --signal"):
        resolve_signal("vibes", sdd_dir=tmp_path / ".sdd")


def test_predicate_round_trips_through_embedded_params(tmp_path: Path) -> None:
    """The receipt's embedded signal block rebuilds the identical predicate."""
    gate_dir = tmp_path / ".sdd" / "eval" / "gate"
    _write_gate_receipt(gate_dir, timestamp=100)
    original = gate_signal(None, gate_dir=gate_dir)

    rebuilt = predicate_from_params(json.loads(json.dumps(original.params)))

    assert rebuilt.predicate_id == original.predicate_id
    assert rebuilt.needles == original.needles
    assert rebuilt.predicate_hash() == original.predicate_hash()


def test_predicate_from_params_rejects_unknown_kind() -> None:
    with pytest.raises(DiagnoseError, match="unknown signal kind"):
        predicate_from_params({"kind": "mystery"})
