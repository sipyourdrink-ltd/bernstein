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


def test_shared_loader_owns_the_malformed_line_policy(tmp_path: Path) -> None:
    """One scan implementation serves both policies: journal.load_events is
    tolerant by default and strict on request, and diagnose delegates to it,
    so the two readers can never drift (regression for bot-ack: 3706042994)."""
    from bernstein.core.replay.journal import JournalParseError

    sdd = tmp_path / ".sdd"
    path = _seed_journal(sdd, "run-policy", bad_step=None, steps=3)
    with path.open("a", encoding="utf-8") as f:
        f.write("{garbage\n")

    tolerant = load_events(path)
    assert len(tolerant) == 3  # ordinary readers keep their torn-tail tolerance

    with pytest.raises(JournalParseError, match="unparsable line at physical line 3"):
        load_events(path, strict=True)


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
# artefact signal (lineage log must pass the gate before shaping a predicate)
# ---------------------------------------------------------------------------

_LINEAGE_SECRET = b"op-secret-diagnose"


class _LineageAgent:
    """A signing identity with an on-disk agent card, as the gate expects."""

    def __init__(self) -> None:
        from bernstein.core.lineage.identity import generate_keypair

        self.agent_id = "agent:diag"
        self.kid = "k1"
        self.priv, self.pub = generate_keypair()


def _signed_entry(
    agent: _LineageAgent,
    path: str,
    *,
    kind: str,
    content: bytes,
    parents: list[str],
    ts_ns: int,
    trust_class: str | None = None,
) -> LineageEntry:
    def build(op_hmac: str) -> LineageEntry:
        return LineageEntry(
            v=1,
            artefact_path=path,
            artefact_kind=kind,
            content_hash="sha256:" + hashlib.sha256(content).hexdigest(),
            parent_hashes=parents,
            agent_id=agent.agent_id,
            agent_card_kid=agent.kid,
            tool_call_id="tc-1",
            span_id="span-1",
            ts_ns=ts_ns,
            operator_hmac=op_hmac,
            trust_class=trust_class,
        )

    from bernstein.core.lineage.entry import compute_operator_hmac

    return build(compute_operator_hmac(build(""), _LINEAGE_SECRET))


def _write_gated_log(root: Path, entries: list[LineageEntry], agent: _LineageAgent) -> tuple[Path, Path]:
    """Write a byte-canonical, JWS-signed lineage log + cards; returns paths."""
    from bernstein.core.lineage.entry import canonicalise
    from bernstein.core.lineage.identity import sign_detached

    cards_dir = root / "agents"
    card_dir = cards_dir / agent.agent_id
    card_dir.mkdir(parents=True, exist_ok=True)
    (card_dir / "card.json").write_text(
        json.dumps(
            {
                "protocolVersion": "a2a/1.0",
                "agent_id": agent.agent_id,
                "kid": agent.kid,
                "public_key_pem": agent.pub,
            }
        ),
        encoding="utf-8",
    )

    log_path = root / "lineage" / "log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("wb") as f:
        for e in entries:
            f.write(canonicalise(e) + b"\n")
    sig_root = log_path.parent / "signatures"
    for e in entries:
        jws = sign_detached(canonicalise(e), agent.priv, kid=agent.kid)
        path_hash = hashlib.sha256(e.artefact_path.encode()).hexdigest()
        dest = sig_root / path_hash[:2] / path_hash
        dest.mkdir(parents=True, exist_ok=True)
        (dest / (entry_hash(e).replace("sha256:", "") + ".jws")).write_text(jws, encoding="utf-8")
    return log_path, cards_dir


def _tainted_pair(agent: _LineageAgent) -> tuple[LineageEntry, LineageEntry]:
    tainted = _signed_entry(
        agent,
        "provenance/web.fetch/aaaa",
        kind="tool-result",
        content=b"outsider-bytes",
        parents=[],
        ts_ns=1,
        trust_class="third_party",
    )
    tip = _signed_entry(
        agent,
        "out.txt",
        kind="file",
        content=b"derived-bytes",
        parents=[entry_hash(tainted)],
        ts_ns=2,
    )
    return tainted, tip


def test_artefact_signal_locates_first_appearance_of_tainted_hash(tmp_path: Path) -> None:
    """Lineage walks back from the tip; the culprit step first records the
    tainted record's content hash, and the parent chain rides as evidence."""
    sdd = tmp_path / ".sdd"
    agent = _LineageAgent()
    tainted, tip = _tainted_pair(agent)
    log_path, cards_dir = _write_gated_log(sdd, [tainted, tip], agent)

    predicate = artefact_signal("out.txt", lineage_log=log_path, cards_dir=cards_dir, operator_secret=_LINEAGE_SECRET)
    tainted_hex = tainted.content_hash.split(":", 1)[-1]
    assert predicate.needles == (tainted_hex,)
    assert predicate.lineage_path == (entry_hash(tainted), entry_hash(tip))
    assert predicate.params["lineage_gate"] == {"checked": True, "operator_hmac_checked": True}

    journal = EventJournal("run-art", sdd)
    journal.record("tick", step=0)
    journal.record("tick", step=1)
    journal.record("tool_result", content_hash=tainted.content_hash)
    journal.record("tick", step=3)

    result = diagnose_run(journal.path, predicate, run_id="run-art")

    assert result.culprit_index == 2
    assert result.reason_code == REASON_CODE_BAD_INPUT_CONTENT_HASH
    assert result.lineage_path == (entry_hash(tainted), entry_hash(tip))


def test_unsigned_lineage_entry_cannot_shape_a_sealed_diagnosis(tmp_path: Path) -> None:
    """The gate runs before any predicate is shaped: an unsigned log and a
    tampered signed log both refuse (regression for bot-ack: 3706042986)."""
    agent = _LineageAgent()
    tainted, tip = _tainted_pair(agent)

    # Unsigned: entries on disk but no JWS sidecars and no canonical bytes.
    unsigned_log = tmp_path / "unsigned" / "lineage" / "log.jsonl"
    unsigned_log.parent.mkdir(parents=True)
    unsigned_log.write_text("\n".join(json.dumps(asdict(e)) for e in (tainted, tip)) + "\n", encoding="utf-8")
    with pytest.raises(DiagnoseError, match="lineage gate failed"):
        artefact_signal(
            "out.txt",
            lineage_log=unsigned_log,
            cards_dir=tmp_path / "unsigned" / "agents",
            operator_secret=_LINEAGE_SECRET,
        )

    # Tampered: properly signed log with one byte flipped afterwards.
    log_path, cards_dir = _write_gated_log(tmp_path / "tampered", [tainted, tip], agent)
    raw = log_path.read_bytes().replace(b"third_party", b"first_party", 1)
    log_path.write_bytes(raw)
    with pytest.raises(DiagnoseError, match="lineage gate failed"):
        artefact_signal("out.txt", lineage_log=log_path, cards_dir=cards_dir, operator_secret=_LINEAGE_SECRET)


def test_artefact_gate_mode_is_disclosed_in_sealed_params(tmp_path: Path) -> None:
    """Without an operator secret the gate still verifies signatures, and the
    weaker mode is disclosed in the params the receipt seals."""
    agent = _LineageAgent()
    tainted, tip = _tainted_pair(agent)
    log_path, cards_dir = _write_gated_log(tmp_path, [tainted, tip], agent)

    predicate = artefact_signal("out.txt", lineage_log=log_path, cards_dir=cards_dir, operator_secret=None)

    assert predicate.params["lineage_gate"] == {"checked": True, "operator_hmac_checked": False}


def test_artefact_signal_refuses_untainted_artefact(tmp_path: Path) -> None:
    agent = _LineageAgent()
    clean = _signed_entry(agent, "ok.txt", kind="file", content=b"fine", parents=[], ts_ns=1, trust_class="operator")
    log_path, cards_dir = _write_gated_log(tmp_path, [clean], agent)

    with pytest.raises(DiagnoseError, match="not tainted"):
        artefact_signal("ok.txt", lineage_log=log_path, cards_dir=cards_dir, operator_secret=_LINEAGE_SECRET)


def test_artefact_signal_fails_closed_without_lineage(tmp_path: Path) -> None:
    with pytest.raises(DiagnoseError, match="no lineage entries"):
        artefact_signal("out.txt", lineage_log=tmp_path / "missing.jsonl", cards_dir=tmp_path / "agents")


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


def test_mistyped_chain_field_is_refused_as_malformed_not_chain_break(tmp_path: Path) -> None:
    """A row with a wrong-typed chain field is malformed input, not tamper.

    Pre-fix, strict loading accepted any JSON object, so a mistyped row
    reached verify_journal and was reported as a cryptographic chain break
    at that index -- the honest verdict is a refusal naming the physical
    line (regression for bot-ack: 3707430834).
    """
    import json as _json

    from bernstein.core.replay.journal import JournalParseError

    sdd = tmp_path / ".sdd"
    path = _seed_journal(sdd, "run-mistyped", bad_step=None, steps=3)
    rows = [_json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows[1]["index"] = str(rows[1]["index"])  # right value, wrong type
    path.write_text("".join(_json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    with pytest.raises(JournalParseError, match="physical line 1 has a missing or non-integer 'index'"):
        load_events(path, strict=True)


def test_row_missing_event_hash_is_refused_before_any_chain_verdict(tmp_path: Path) -> None:
    import json as _json

    from bernstein.core.replay.journal import JournalParseError

    sdd = tmp_path / ".sdd"
    path = _seed_journal(sdd, "run-nohash", bad_step=None, steps=3)
    rows = [_json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    del rows[2]["event_hash"]
    path.write_text("".join(_json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    with pytest.raises(JournalParseError, match="physical line 2 has a missing or empty 'event_hash'"):
        load_events(path, strict=True)


def test_string_needles_in_receipt_params_are_refused_not_split() -> None:
    """A plain-string needles value must refuse, never iterate per character.

    Pre-fix, tuple(str(n) for n in "abc") silently rebuilt the predicate as
    ("a", "b", "c") -- a predicate that was never evaluated -- and its
    predicate_hash changed with it (regression for bot-ack: 3707430843).
    """
    with pytest.raises(DiagnoseError, match="'needles' must be a list of strings"):
        predicate_from_params({"kind": "gate", "needles": "abc"})


def test_malformed_lineage_gate_params_are_refused() -> None:
    with pytest.raises(DiagnoseError, match="'lineage_gate' must be a mapping of booleans"):
        predicate_from_params(
            {"kind": "artefact", "needles": [], "lineage_path": [], "lineage_gate": {"checked": "yes"}}
        )


def test_predicate_from_params_rejects_unknown_kind() -> None:
    with pytest.raises(DiagnoseError, match="unknown signal kind"):
        predicate_from_params({"kind": "mystery"})
