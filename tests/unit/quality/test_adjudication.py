"""Tests for maker-checker + judge-panel adjudication records (issue #2294).

Each gate verdict is a signed adjudication record

    {inputs_hash, rubric_hash, panel_config, per_judge_verdict, final_verdict}

anchored to the run journal (the lineage spine). The record is the primary
artefact: strip the spine anchor and the record loses its meaning, not just
its log. Panels must be genuinely independent - two judges sharing
model+temp+prompt are rejected so a panel cannot agree on the same error.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.quality.adjudication import (
    AdjudicationRecord,
    CostMode,
    JudgeConfig,
    JudgeVerdict,
    PanelConfig,
    PanelIndependenceError,
    Verdict,
    adjudicate,
    attribute_cost,
    verify_adjudication,
)

_KEY = b"k" * 32


def _judge(model: str, temp: float, prompt: str, verdict: Verdict = Verdict.PASS) -> tuple[JudgeConfig, JudgeVerdict]:
    cfg = JudgeConfig(model=model, temperature=temp, prompt_hash=prompt)
    return cfg, JudgeVerdict(config=cfg, verdict=verdict, rationale_hash="r-" + model)


# ---------------------------------------------------------------------------
# Independence (AC2)
# ---------------------------------------------------------------------------


def test_panel_with_identical_judge_configs_is_rejected() -> None:
    cfg_a = JudgeConfig(model="m1", temperature=0.0, prompt_hash="p")
    cfg_b = JudgeConfig(model="m1", temperature=0.0, prompt_hash="p")
    with pytest.raises(PanelIndependenceError):
        PanelConfig(judges=(cfg_a, cfg_b))


def test_panel_distinct_on_any_axis_is_accepted() -> None:
    # Differ only by temperature.
    PanelConfig(
        judges=(
            JudgeConfig(model="m1", temperature=0.0, prompt_hash="p"),
            JudgeConfig(model="m1", temperature=0.7, prompt_hash="p"),
        )
    )
    # Differ only by prompt.
    PanelConfig(
        judges=(
            JudgeConfig(model="m1", temperature=0.0, prompt_hash="p"),
            JudgeConfig(model="m1", temperature=0.0, prompt_hash="q"),
        )
    )
    # Differ only by model.
    PanelConfig(
        judges=(
            JudgeConfig(model="m1", temperature=0.0, prompt_hash="p"),
            JudgeConfig(model="m2", temperature=0.0, prompt_hash="p"),
        )
    )


def test_maker_checker_requires_two_distinct_roles() -> None:
    same = JudgeConfig(model="m", temperature=0.0, prompt_hash="p")
    with pytest.raises(PanelIndependenceError):
        PanelConfig(judges=(same, same))


# ---------------------------------------------------------------------------
# Anchoring + record shape (AC4)
# ---------------------------------------------------------------------------


def test_adjudicate_anchors_record_to_journal(tmp_path: Path) -> None:
    cfg_m, v_m = _judge("cheap", 0.0, "maker")
    cfg_c, v_c = _judge("capable", 0.0, "checker")
    panel = PanelConfig(judges=(cfg_m, cfg_c))

    record = adjudicate(
        run_id="run-1",
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        inputs={"diff": "abc", "task": "t1"},
        rubric={"rule": "no-regressions"},
        panel=panel,
        judge_verdicts=(v_m, v_c),
        now=1234,
    )
    assert isinstance(record, AdjudicationRecord)
    assert record.journal_entry_hash  # anchored
    assert record.final_verdict is Verdict.PASS
    assert len(record.per_judge_verdict) == 2
    assert record.inputs_hash.startswith("sha256:")
    assert record.rubric_hash.startswith("sha256:")


# ---------------------------------------------------------------------------
# Verify recomputes inputs_hash (AC3)
# ---------------------------------------------------------------------------


def test_verify_detects_tampered_inputs(tmp_path: Path) -> None:
    cfg_m, v_m = _judge("cheap", 0.0, "maker")
    cfg_c, v_c = _judge("capable", 0.0, "checker")
    panel = PanelConfig(judges=(cfg_m, cfg_c))
    root = tmp_path / ".sdd" / "lineage"

    record = adjudicate(
        run_id="run-1",
        lineage_root=root,
        hmac_key=_KEY,
        inputs={"diff": "abc"},
        rubric={"rule": "r"},
        panel=panel,
        judge_verdicts=(v_m, v_c),
        now=1234,
    )

    ok = verify_adjudication(
        run_id="run-1",
        lineage_root=root,
        hmac_key=_KEY,
        record=record,
        claimed_inputs={"diff": "abc"},
    )
    assert ok.ok, ok.reason

    bad = verify_adjudication(
        run_id="run-1",
        lineage_root=root,
        hmac_key=_KEY,
        record=record,
        claimed_inputs={"diff": "TAMPERED"},
    )
    assert not bad.ok
    assert "inputs" in bad.reason.lower()


def test_verify_detects_spine_tamper(tmp_path: Path) -> None:
    cfg_m, v_m = _judge("cheap", 0.0, "maker")
    cfg_c, v_c = _judge("capable", 0.0, "checker")
    panel = PanelConfig(judges=(cfg_m, cfg_c))
    root = tmp_path / ".sdd" / "lineage"

    record = adjudicate(
        run_id="run-1",
        lineage_root=root,
        hmac_key=_KEY,
        inputs={"diff": "abc"},
        rubric={"rule": "r"},
        panel=panel,
        judge_verdicts=(v_m, v_c),
        now=1234,
    )

    # Corrupt the spine on disk (flip a byte in the stored content hash).
    spine_file = root / "run-1" / "spine.jsonl"
    raw = spine_file.read_bytes()
    marker = b'"content_hash":"sha256:'
    idx = raw.index(marker) + len(marker)
    flipped = b"0" if raw[idx : idx + 1] != b"0" else b"1"
    spine_file.write_bytes(raw[:idx] + flipped + raw[idx + 1 :])

    result = verify_adjudication(
        run_id="run-1",
        lineage_root=root,
        hmac_key=_KEY,
        record=record,
        claimed_inputs={"diff": "abc"},
    )
    assert not result.ok


# ---------------------------------------------------------------------------
# Determinism (AC5)
# ---------------------------------------------------------------------------


def test_two_replays_produce_identical_records(tmp_path: Path) -> None:
    cfg_m, v_m = _judge("cheap", 0.0, "maker")
    cfg_c, v_c = _judge("capable", 0.0, "checker")
    panel = PanelConfig(judges=(cfg_m, cfg_c))

    def _run(root: Path) -> AdjudicationRecord:
        return adjudicate(
            run_id="run-1",
            lineage_root=root,
            hmac_key=_KEY,
            inputs={"diff": "abc", "task": "t1"},
            rubric={"rule": "r"},
            panel=panel,
            judge_verdicts=(v_m, v_c),
            now=1234,
        )

    r1 = _run(tmp_path / "a" / ".sdd" / "lineage")
    r2 = _run(tmp_path / "b" / ".sdd" / "lineage")
    assert r1.to_canonical_bytes() == r2.to_canonical_bytes()
    assert r1.journal_entry_hash == r2.journal_entry_hash


# ---------------------------------------------------------------------------
# Panel aggregation
# ---------------------------------------------------------------------------


def test_panel_majority_fail(tmp_path: Path) -> None:
    _, v1 = _judge("m1", 0.0, "p1", Verdict.FAIL)
    _, v2 = _judge("m2", 0.0, "p2", Verdict.FAIL)
    _, v3 = _judge("m3", 0.0, "p3", Verdict.PASS)
    panel = PanelConfig(judges=(v1.config, v2.config, v3.config))

    record = adjudicate(
        run_id="run-p",
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        inputs={"x": 1},
        rubric={"r": 1},
        panel=panel,
        judge_verdicts=(v1, v2, v3),
        now=1,
    )
    assert record.final_verdict is Verdict.FAIL


def test_maker_checker_checker_veto(tmp_path: Path) -> None:
    # Maker passes, checker fails -> final FAIL (checker vetoes).
    _, v_m = _judge("cheap", 0.0, "maker", Verdict.PASS)
    _, v_c = _judge("capable", 0.0, "checker", Verdict.FAIL)
    panel = PanelConfig(judges=(v_m.config, v_c.config), mode="maker_checker")

    record = adjudicate(
        run_id="run-mc",
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        inputs={"x": 1},
        rubric={"r": 1},
        panel=panel,
        judge_verdicts=(v_m, v_c),
        now=1,
    )
    assert record.final_verdict is Verdict.FAIL


# ---------------------------------------------------------------------------
# Cost mode (cheap-maker / capable-checker)
# ---------------------------------------------------------------------------


def test_cost_mode_attributes_maker_and_checker(tmp_path: Path) -> None:
    from bernstein.core.cost.spend_ledger import SpendLedger

    ledger = SpendLedger(path=tmp_path / "ledger.jsonl", run_id="run-cost", budget_usd=10.0)
    attribute_cost(
        ledger=ledger,
        mode=CostMode.CHEAP_MAKER_CAPABLE_CHECKER,
        maker_model="cheap",
        maker_cost_usd=0.001,
        checker_model="capable",
        checker_cost_usd=0.02,
        task_id="t1",
    )
    totals = ledger.totals_by("feature_label")
    assert totals.get("adjudication.maker", 0.0) == pytest.approx(0.001)
    assert totals.get("adjudication.checker", 0.0) == pytest.approx(0.02)
