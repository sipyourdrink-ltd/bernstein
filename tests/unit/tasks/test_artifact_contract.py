"""Artifact contract slice 1: kinds, canonical serialisation, typed criteria.

Covers :mod:`bernstein.core.tasks.artifacts`:

* the ``ArtifactKind`` closed set;
* the one-shared-core canonicalisers (text / JSONL / JSON-object) and their
  reject-don't-repair NFC + newline-normalisation policy;
* ``content_hash`` and the cross-run byte-identity property that makes the
  hash a deterministic content-addressed identity;
* the three closed criterion evaluators (``hash_stable`` / ``schema_valid`` /
  ``criteria_match``);
* ``ArtifactSpec`` / ``ArtifactCriterion`` round-trips.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata

import pytest

from bernstein.core.tasks.artifacts import (
    ArtifactCriterion,
    ArtifactKind,
    ArtifactSpec,
    CanonicalisationError,
    artifact_content_hash,
    canonicalise_artifact,
    content_hash,
    evaluate_criterion,
)

# ---------------------------------------------------------------------------
# ArtifactKind closed set
# ---------------------------------------------------------------------------


def test_artifact_kind_membership() -> None:
    assert {k.value for k in ArtifactKind} == {
        "code_diff",
        "report",
        "dataset",
        "action_log",
        "ops_result",
    }


def test_artifact_kind_coerces_from_str() -> None:
    assert ArtifactKind("report") is ArtifactKind.REPORT


# ---------------------------------------------------------------------------
# Text canonical core (report / code_diff)
# ---------------------------------------------------------------------------


def test_report_text_is_utf8_with_normalised_newlines() -> None:
    raw = "line1\r\nline2\rline3\n"
    canon = canonicalise_artifact(ArtifactKind.REPORT, raw)
    assert canon == b"line1\nline2\nline3\n"


def test_code_diff_shares_the_text_core() -> None:
    raw = "@@ -1 +1 @@\r\n-old\r\n+new\r\n"
    assert canonicalise_artifact(ArtifactKind.CODE_DIFF, raw) == b"@@ -1 +1 @@\n-old\n+new\n"


def test_text_bytes_input_is_decoded_utf8() -> None:
    assert canonicalise_artifact(ArtifactKind.REPORT, "café".encode()) == "café".encode()


def test_text_rejects_non_nfc_rather_than_repairing() -> None:
    # "e" + combining acute accent is NFD, not NFC.
    nfd = "café"
    assert not unicodedata.is_normalized("NFC", nfd)
    with pytest.raises(CanonicalisationError, match="NFC"):
        canonicalise_artifact(ArtifactKind.REPORT, nfd)


def test_text_rejects_invalid_utf8_bytes() -> None:
    with pytest.raises(CanonicalisationError, match="UTF-8"):
        canonicalise_artifact(ArtifactKind.REPORT, b"\xff\xfe")


# ---------------------------------------------------------------------------
# JSONL canonical core (dataset / action_log)
# ---------------------------------------------------------------------------


def test_dataset_is_canonical_jsonl_one_object_per_line() -> None:
    rows = [{"b": 2, "a": 1}, {"z": 26, "y": 25}]
    canon = canonicalise_artifact(ArtifactKind.DATASET, rows)
    assert canon == b'{"a":1,"b":2}\n{"y":25,"z":26}'


def test_action_log_key_order_is_stable_regardless_of_input_order() -> None:
    a = canonicalise_artifact(ArtifactKind.ACTION_LOG, [{"x": 1, "m": 2, "a": 3}])
    b = canonicalise_artifact(ArtifactKind.ACTION_LOG, [{"a": 3, "x": 1, "m": 2}])
    assert a == b == b'{"a":3,"m":2,"x":1}'


def test_empty_dataset_is_empty_bytes() -> None:
    assert canonicalise_artifact(ArtifactKind.DATASET, []) == b""


def test_dataset_rejects_scalar_and_mapping() -> None:
    with pytest.raises(CanonicalisationError):
        canonicalise_artifact(ArtifactKind.DATASET, {"not": "a list"})
    with pytest.raises(CanonicalisationError):
        canonicalise_artifact(ArtifactKind.DATASET, "scalar")


def test_jsonl_rejects_nan() -> None:
    with pytest.raises(CanonicalisationError):
        canonicalise_artifact(ArtifactKind.DATASET, [{"v": float("nan")}])


# ---------------------------------------------------------------------------
# JSON-object canonical core (ops_result)
# ---------------------------------------------------------------------------


def test_ops_result_is_single_canonical_object() -> None:
    canon = canonicalise_artifact(ArtifactKind.OPS_RESULT, {"status": "ok", "changed": 3, "applied": True})
    assert canon == b'{"applied":true,"changed":3,"status":"ok"}'


# ---------------------------------------------------------------------------
# content_hash + cross-run byte identity (determinism heart)
# ---------------------------------------------------------------------------


def test_content_hash_is_sha256_of_canonical_bytes() -> None:
    canon = canonicalise_artifact(ArtifactKind.REPORT, "hello\n")
    assert content_hash(canon) == "sha256:" + hashlib.sha256(canon).hexdigest()


def test_same_input_double_run_is_byte_identical() -> None:
    rows = [{"id": 2, "name": "b"}, {"id": 1, "name": "a"}]
    first = artifact_content_hash(ArtifactKind.DATASET, rows)
    second = artifact_content_hash(ArtifactKind.DATASET, [dict(r) for r in rows])
    assert first == second


def test_one_byte_mutation_changes_the_hash() -> None:
    base = artifact_content_hash(ArtifactKind.REPORT, "the quick brown fox\n")
    mutated = artifact_content_hash(ArtifactKind.REPORT, "the quick brown frx\n")
    assert base != mutated


# ---------------------------------------------------------------------------
# hash_stable criterion
# ---------------------------------------------------------------------------


def test_hash_stable_passes_on_matching_hash() -> None:
    rows = [{"a": 1}]
    expected = artifact_content_hash(ArtifactKind.DATASET, rows)
    ok, _ = evaluate_criterion("hash_stable", expected, artifact=rows, kind=ArtifactKind.DATASET)
    assert ok


def test_hash_stable_fails_on_drifted_input() -> None:
    expected = artifact_content_hash(ArtifactKind.DATASET, [{"a": 1}])
    ok, detail = evaluate_criterion("hash_stable", expected, artifact=[{"a": 2}], kind=ArtifactKind.DATASET)
    assert not ok
    assert "drift" in detail


# ---------------------------------------------------------------------------
# schema_valid criterion
# ---------------------------------------------------------------------------


def test_schema_valid_passes_for_conforming_rows() -> None:
    schema = json.dumps({"type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}}})
    rows = [{"id": 1}, {"id": 2}]
    ok, _ = evaluate_criterion("schema_valid", schema, artifact=rows, kind=ArtifactKind.DATASET)
    assert ok


def test_schema_valid_fails_for_nonconforming_row() -> None:
    schema = json.dumps({"type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}}})
    rows = [{"id": 1}, {"name": "no id"}]
    ok, detail = evaluate_criterion("schema_valid", schema, artifact=rows, kind=ArtifactKind.DATASET)
    assert not ok
    assert "row 1" in detail


def test_schema_valid_on_ops_result_object() -> None:
    schema = json.dumps({"type": "object", "required": ["status"]})
    ok, _ = evaluate_criterion("schema_valid", schema, artifact={"status": "ok"}, kind=ArtifactKind.OPS_RESULT)
    assert ok


def test_schema_valid_fails_on_text_kind_without_json_document() -> None:
    schema = json.dumps({"type": "object"})
    ok, detail = evaluate_criterion("schema_valid", schema, artifact="prose", kind=ArtifactKind.REPORT)
    assert not ok
    assert "JSON document" in detail


# ---------------------------------------------------------------------------
# criteria_match criterion
# ---------------------------------------------------------------------------


def test_criteria_match_predicate_set_passes() -> None:
    preds = json.dumps(
        [
            {"path": "status", "op": "eq", "value": "ok"},
            {"path": "changed", "op": "gt", "value": 0},
            {"path": "detail", "op": "exists"},
        ]
    )
    art = {"status": "ok", "changed": 3, "detail": "x"}
    ok, _ = evaluate_criterion("criteria_match", preds, artifact=art, kind=ArtifactKind.OPS_RESULT)
    assert ok


def test_criteria_match_fails_and_names_predicate() -> None:
    preds = json.dumps([{"path": "status", "op": "eq", "value": "ok"}])
    ok, detail = evaluate_criterion("criteria_match", preds, artifact={"status": "error"}, kind=ArtifactKind.OPS_RESULT)
    assert not ok
    assert "predicate 0" in detail


def test_criteria_match_rejects_unknown_op() -> None:
    preds = json.dumps([{"path": "x", "op": "regex", "value": ".*"}])
    ok, detail = evaluate_criterion("criteria_match", preds, artifact={"x": 1}, kind=ArtifactKind.OPS_RESULT)
    assert not ok
    assert "unknown op" in detail


def test_criteria_match_indexes_into_jsonl_rows() -> None:
    preds = json.dumps([{"path": "0.id", "op": "eq", "value": 7}])
    ok, _ = evaluate_criterion("criteria_match", preds, artifact=[{"id": 7}], kind=ArtifactKind.DATASET)
    assert ok


def test_unknown_criterion_type_returns_false() -> None:
    ok, detail = evaluate_criterion("bogus", "x", artifact={"a": 1}, kind=ArtifactKind.OPS_RESULT)
    assert not ok
    assert "criterion type" in detail


# ---------------------------------------------------------------------------
# ArtifactCriterion + ArtifactSpec round-trips
# ---------------------------------------------------------------------------


def test_artifact_criterion_round_trip() -> None:
    crit = ArtifactCriterion(type="hash_stable", value="sha256:abc")
    assert ArtifactCriterion.from_dict(crit.to_dict()) == crit


def test_artifact_criterion_rejects_unknown_type() -> None:
    with pytest.raises(ValueError, match="criterion type"):
        ArtifactCriterion(type="path_exists", value="x")  # type: ignore[arg-type]


def test_artifact_spec_default_is_code_diff() -> None:
    spec = ArtifactSpec()
    assert spec.kind is ArtifactKind.CODE_DIFF
    assert spec.canonical_rule == "code_diff"
    assert spec.criteria == ()


def test_artifact_spec_round_trip_with_criteria() -> None:
    spec = ArtifactSpec(
        kind=ArtifactKind.DATASET,
        canonicalisation="",
        criteria=(
            ArtifactCriterion(type="hash_stable", value="sha256:deadbeef"),
            ArtifactCriterion(type="schema_valid", value='{"type":"object"}'),
        ),
    )
    restored = ArtifactSpec.from_dict(spec.to_dict())
    assert restored == spec
    assert restored.canonical_rule == "dataset"


def test_artifact_spec_from_dict_coerces_kind_string() -> None:
    spec = ArtifactSpec.from_dict({"kind": "action_log"})
    assert spec.kind is ArtifactKind.ACTION_LOG
    assert spec.criteria == ()


def test_artifact_spec_constructor_coerces_kind_string() -> None:
    spec = ArtifactSpec(kind="report")  # type: ignore[arg-type]
    assert spec.kind is ArtifactKind.REPORT
