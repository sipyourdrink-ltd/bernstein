"""Unit tests for ``bernstein.sdd.validator``.

Coverage:

- Schema load: cache, version normalisation, error paths.
- Frontmatter parser: fenced markdown, raw YAML, BOM, empty body, malformed YAML.
- Validation: each required-key omission, enum violations, format checks,
  recommended-key warnings, strict mode promotion, nested mappings.
- File-on-disk paths: missing file, directory, unreadable, .yaml extension.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from enum import StrEnum
from importlib import resources
from pathlib import Path
from threading import Barrier, Event, Lock
from typing import Any
from unittest.mock import patch

import jsonschema
import pytest

from bernstein.sdd import (
    SchemaNotFoundError,
    ValidationIssue,
    ValidationReport,
    list_recommended_keys,
    load_schema,
    validate_ticket,
    validate_ticket_metadata,
)
from bernstein.sdd import validator as validator_module
from bernstein.sdd.validator import (
    RECOMMENDED_KEYS,
    parse_frontmatter,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "sdd_tickets"


def _minimal_meta() -> dict[str, Any]:
    return {
        "id": "feat-2026-05-17-min",
        "created": "2026-05-17",
        "status": "open",
        "priority": "P1",
        "effort": "M",
    }


# ---------------------------------------------------------------------------
# Schema loading (5 tests)
# ---------------------------------------------------------------------------


def test_load_schema_v1_returns_object() -> None:
    schema = load_schema("v1")
    assert isinstance(schema, dict)
    assert schema["$schema"].startswith("http://json-schema.org/draft-07")
    assert "id" in schema["required"]


def test_load_schema_caches_result() -> None:
    a = load_schema("v1")
    b = load_schema("v1")
    assert a is b


def test_warm_validator_reuses_packaged_definition(monkeypatch: pytest.MonkeyPatch) -> None:
    # A fresh packaged lookup also exercises the first construction in a warm test process.
    monkeypatch.setattr(validator_module, "_SCHEMA_CACHE", {})
    validator_module._validator_for_definition.cache_clear()
    with patch.object(jsonschema, "Draft7Validator", wraps=jsonschema.Draft7Validator) as construct:
        assert validate_ticket_metadata(_minimal_meta()).ok
        assert load_schema() is load_schema()
        invalid = {**_minimal_meta(), "created": "2026-02-30", "priority": "invalid"}
        report = validate_ticket_metadata(invalid)
        strict = validate_ticket_metadata(invalid, strict=True)
        assert [(i.path, i.code) for i in report.errors] == [(("created",), "format"), (("priority",), "enum")]
        assert [i.path for i in report.warnings] == [(key,) for key in RECOMMENDED_KEYS]
        assert strict.errors == report.errors + report.warnings
        assert strict.warnings == []
        with pytest.raises(SchemaNotFoundError):
            validate_ticket_metadata(invalid, schema_version="v999")
        assert construct.call_count == 1


def test_warm_validator_observes_changed_definition(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Isolate the public mutable schema from other users in this test process.
    monkeypatch.setattr(validator_module, "_SCHEMA_CACHE", {})
    schema = load_schema()
    assert validate_ticket_metadata(_minimal_meta()).ok
    assert load_schema() is schema
    schema["required"] = [*schema["required"], "synthetic"]
    assert [i.code for i in validate_ticket_metadata(_minimal_meta()).errors] == ["required"]

    replacement = deepcopy(schema)
    replacement["required"].remove("synthetic")
    replacement["properties"]["priority"]["enum"] = ["custom"]
    (tmp_path / "ticket.v1.json").write_text(json.dumps(replacement), encoding="utf-8")
    monkeypatch.setattr(resources, "files", lambda _package: tmp_path)
    monkeypatch.setattr(validator_module, "_SCHEMA_CACHE", {})
    assert [(i.path, i.code) for i in validate_ticket_metadata(_minimal_meta()).errors] == [(("priority",), "enum")]
    assert validate_ticket_metadata({**_minimal_meta(), "priority": "custom"}).ok


def test_mutable_schema_preserves_diagnostic_order_and_python_annotations(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(validator_module, "_SCHEMA_CACHE", {})
    schema = load_schema()
    invalid = {**_minimal_meta(), "priority": "invalid", "created": "unknown"}
    assert [i.path for i in validate_ticket_metadata(invalid).errors] == [("created",), ("priority",)]
    properties = schema["properties"]
    properties["created"] = properties.pop("created")
    assert [i.path for i in validate_ticket_metadata(invalid).errors] == [("priority",), ("created",)]
    schema["x-annotation"] = object()
    assert [i.path for i in validate_ticket_metadata(invalid).errors] == [("priority",), ("created",)]


def test_overlapping_public_lookups_keep_both_callers_edits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(validator_module, "_SCHEMA_CACHE", {})
    assert validate_ticket_metadata(_minimal_meta()).ok
    first_decode = True
    guard, returned = Lock(), Event()
    start = Barrier(2)
    decode = json.loads

    def overlapping_decode(*args, **kwargs):
        nonlocal first_decode
        with guard:
            wait_for_other, first_decode = first_decode, False
        if wait_for_other:
            # Expose simultaneous lazy decoding; a serialized decoder may finish alone.
            returned.wait(1)
        return decode(*args, **kwargs)

    def lookup():
        start.wait(timeout=5)
        schema = load_schema()
        returned.set()
        return schema

    with monkeypatch.context() as mp, ThreadPoolExecutor(max_workers=2) as pool:
        mp.setattr(json, "loads", overlapping_decode)
        first, second = [future.result(timeout=5) for future in (pool.submit(lookup), pool.submit(lookup))]
    first["required"].append("field_one")
    second["required"].append("field_two")
    assert first is second and load_schema() is first
    assert [issue.message for issue in validate_ticket_metadata(_minimal_meta()).errors] == [
        "'field_one' is a required property",
        "'field_two' is a required property",
    ]


class _Severity(StrEnum):
    HIGH = "high"


@pytest.mark.parametrize(
    ("rule", "value"),
    [
        ({"const": {1: "one"}}, {"1": "one"}),
        ({"const": {1: "one"}}, {1: "one"}),
        ({"enum": [_Severity.HIGH]}, "low"),
        ({"enum": [_Severity.HIGH]}, "high"),
        ({"const": "\ud83d\ude00"}, "\U0001f600"),
        ({"const": "\ud83d\ude00"}, "\ud83d\ude00"),
        ({"enum": ["\ud800"]}, "\ud800"),
        ({"type": "object", "required": ["\ud83d\ude00"]}, {"\U0001f600": 1}),
        ({"type": "object", "properties": {"\ud83d\ude00": {"type": "integer"}}}, {"\ud83d\ude00": "bad"}),
    ],
)
@pytest.mark.parametrize("strict", [False, True])
def test_python_override_matches_full_draft7_report(
    monkeypatch: pytest.MonkeyPatch, rule: dict, value: Any, strict: bool
) -> None:
    monkeypatch.setattr(validator_module, "_SCHEMA_CACHE", {})
    schema = load_schema()
    assert validate_ticket_metadata(_minimal_meta()).ok
    schema["properties"]["synthetic"] = rule
    jsonschema.Draft7Validator.check_schema(schema)
    metadata = {**_minimal_meta(), "synthetic": value}
    reference = jsonschema.Draft7Validator(schema, format_checker=validator_module._format_checker())
    errors = [
        ValidationIssue(message=e.message, path=tuple(e.absolute_path), code=e.validator)
        for e in reference.iter_errors(metadata)
    ]
    recommended = [
        ValidationIssue(message=f"recommended key missing: {key}", path=(key,), code="recommended_missing")
        for key in RECOMMENDED_KEYS
    ]
    expected = ValidationReport(
        Path("ticket.md"), errors + recommended if strict else errors, [] if strict else recommended
    )
    assert validate_ticket_metadata(metadata, strict=strict, path=expected.path).to_dict() == expected.to_dict()


def test_distinct_versions_with_identical_packaged_bytes_stay_independent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    encoded = resources.files("bernstein.sdd.schema").joinpath("ticket.v1.json").read_bytes()
    for version in ("v1", "v2"):
        (tmp_path / f"ticket.{version}.json").write_bytes(encoded)
    monkeypatch.setattr(resources, "files", lambda _package: tmp_path)
    monkeypatch.setattr(validator_module, "_SCHEMA_CACHE", {})
    validator_module._validator_for_definition.cache_clear()
    with patch.object(jsonschema, "Draft7Validator", wraps=jsonschema.Draft7Validator) as construct:
        for version in ("v1", "v2", "v1", "v2"):
            assert validate_ticket_metadata(_minimal_meta(), schema_version=version).ok
        assert construct.call_count == 2
    first, second = load_schema("v1"), load_schema("v2")
    assert first is not second
    first["required"].append("only_first")
    assert not validate_ticket_metadata(_minimal_meta(), schema_version="v1").ok
    assert validate_ticket_metadata(_minimal_meta(), schema_version="v2").ok


def test_override_equal_to_packaged_json_keeps_its_python_diagnostic(monkeypatch: pytest.MonkeyPatch) -> None:
    class Priority(StrEnum):
        HIGH = "P1"

    monkeypatch.setattr(validator_module, "_SCHEMA_CACHE", {})
    schema = load_schema()
    assert validate_ticket_metadata(_minimal_meta()).ok
    options = schema["properties"]["priority"]["enum"]
    options[options.index("P1")] = Priority.HIGH
    invalid = {**_minimal_meta(), "priority": "P9"}
    reference = jsonschema.Draft7Validator(schema, format_checker=validator_module._format_checker())
    expected = next(reference.iter_errors(invalid))
    assert validate_ticket_metadata(invalid).errors == [
        ValidationIssue(message=expected.message, path=("priority",), code="enum")
    ]


def test_load_schema_unknown_version_raises() -> None:
    with pytest.raises(SchemaNotFoundError):
        load_schema("v999")


def test_load_schema_invalid_version_label_raises() -> None:
    with pytest.raises(SchemaNotFoundError):
        load_schema("not-a-version")


def test_load_schema_resource_importable_from_installed_package() -> None:
    # Equivalent to how a downstream wheel-installed consumer would load it.
    pkg = resources.files("bernstein.sdd.schema")
    found = pkg.joinpath("ticket.v1.json")
    assert found.is_file()
    payload = json.loads(found.read_text(encoding="utf-8"))
    jsonschema.Draft7Validator.check_schema(payload)


# ---------------------------------------------------------------------------
# Recommended keys helper
# ---------------------------------------------------------------------------


def test_recommended_keys_list_is_stable() -> None:
    assert list_recommended_keys("v1") == RECOMMENDED_KEYS
    assert "owner" in RECOMMENDED_KEYS
    assert "rice" in RECOMMENDED_KEYS


def test_recommended_keys_invalid_label_raises() -> None:
    with pytest.raises(SchemaNotFoundError):
        list_recommended_keys("not-a-version")


# ---------------------------------------------------------------------------
# Frontmatter parser
# ---------------------------------------------------------------------------


def test_parse_frontmatter_returns_none_for_empty() -> None:
    assert parse_frontmatter("") is None


def test_parse_frontmatter_returns_none_for_whitespace() -> None:
    assert parse_frontmatter("   \n  \n") is None


def test_parse_frontmatter_fenced_md() -> None:
    text = "---\nid: foo\ncreated: 2026-05-17\n---\nbody\n"
    out = parse_frontmatter(text)
    assert out is not None
    assert out["id"] == "foo"
    assert out["created"] == "2026-05-17"


def test_parse_frontmatter_unclosed_fence_consumes_tail() -> None:
    text = "---\nid: foo\ncreated: 2026-05-17\nstatus: open\n"
    out = parse_frontmatter(text)
    assert out is not None
    assert out["id"] == "foo"
    assert out["status"] == "open"


def test_parse_frontmatter_yaml_only_with_fence() -> None:
    text = "---\nid: foo\nstatus: open\n---\n"
    out = parse_frontmatter(text)
    assert out is not None
    assert out["status"] == "open"


def test_parse_frontmatter_raw_yaml_mapping() -> None:
    out = parse_frontmatter("id: bar\nstatus: open\n")
    assert out is not None
    assert out["id"] == "bar"


def test_parse_frontmatter_returns_none_for_scalar() -> None:
    assert parse_frontmatter("just a string\n") is None


def test_parse_frontmatter_returns_none_for_list() -> None:
    assert parse_frontmatter("- one\n- two\n") is None


def test_parse_frontmatter_handles_bom() -> None:
    text = "﻿---\nid: bom\nstatus: open\n---\n"
    out = parse_frontmatter(text)
    assert out is not None
    assert out["id"] == "bom"


def test_parse_frontmatter_invalid_yaml_returns_none() -> None:
    out = parse_frontmatter("---\nid: : :\n bad: [unterminated\n---\n")
    assert out is None


def test_parse_frontmatter_coerces_date_scalar_to_iso_string() -> None:
    out = parse_frontmatter("---\nid: x\ncreated: 2026-05-17\n---\n")
    assert out is not None
    assert isinstance(out["created"], str)
    assert out["created"] == "2026-05-17"


# ---------------------------------------------------------------------------
# Required keys
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("missing_key", ["id", "created", "status", "priority", "effort"])
def test_required_key_missing_yields_error(missing_key: str) -> None:
    meta = _minimal_meta()
    del meta[missing_key]
    report = validate_ticket_metadata(meta)
    assert not report.ok
    assert any(missing_key in e.message for e in report.errors)


# ---------------------------------------------------------------------------
# Enum and pattern violations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_status", ["wip", "WIP", "done", "", "OPEN"])
def test_invalid_status_rejected(bad_status: str) -> None:
    meta = _minimal_meta()
    meta["status"] = bad_status
    report = validate_ticket_metadata(meta)
    assert not report.ok


@pytest.mark.parametrize("bad_priority", ["P3", "p1", "high", "1"])
def test_invalid_priority_rejected(bad_priority: str) -> None:
    meta = _minimal_meta()
    meta["priority"] = bad_priority
    report = validate_ticket_metadata(meta)
    assert not report.ok


@pytest.mark.parametrize("bad_effort", ["XL", "xl", "small", "1", ""])
def test_invalid_effort_rejected(bad_effort: str) -> None:
    meta = _minimal_meta()
    meta["effort"] = bad_effort
    report = validate_ticket_metadata(meta)
    assert not report.ok


@pytest.mark.parametrize(
    "bad_id",
    ["AB", "x", "BAD", "has space", "has_underscore", "-starts-with-dash", "UPPER-CASE"],
)
def test_invalid_id_pattern_rejected(bad_id: str) -> None:
    meta = _minimal_meta()
    meta["id"] = bad_id
    report = validate_ticket_metadata(meta)
    assert not report.ok


@pytest.mark.parametrize(
    "ok_id",
    ["abc", "feat-x", "2026-05-17-foo", "feat-2026-05-17-bernstein-ticket-validate-cmd"],
)
def test_valid_id_pattern_accepted(ok_id: str) -> None:
    meta = _minimal_meta()
    meta["id"] = ok_id
    report = validate_ticket_metadata(meta, strict=False)
    assert report.ok


def test_invalid_date_rejected() -> None:
    meta = _minimal_meta()
    meta["created"] = "not-a-date"
    report = validate_ticket_metadata(meta)
    assert not report.ok


def test_invalid_date_shape_rejected() -> None:
    meta = _minimal_meta()
    meta["created"] = "2026-13-40"
    report = validate_ticket_metadata(meta)
    assert not report.ok


def test_date_must_be_string_not_int() -> None:
    meta = _minimal_meta()
    meta["created"] = 2026
    report = validate_ticket_metadata(meta)
    assert not report.ok


# ---------------------------------------------------------------------------
# Nested objects (rice / success_metric)
# ---------------------------------------------------------------------------


def test_rice_confidence_out_of_range_rejected() -> None:
    meta = _minimal_meta()
    meta["rice"] = {"reach": 10, "impact": 1, "confidence": 1.5, "effort_days": 1}
    report = validate_ticket_metadata(meta)
    assert not report.ok


def test_rice_effort_days_below_minimum_rejected() -> None:
    meta = _minimal_meta()
    meta["rice"] = {"effort_days": 0.1}
    report = validate_ticket_metadata(meta)
    assert not report.ok


def test_rice_accepted_when_all_in_range() -> None:
    meta = _minimal_meta()
    meta["rice"] = {"reach": 10, "impact": 1.5, "confidence": 0.8, "effort_days": 1, "score": 12}
    report = validate_ticket_metadata(meta)
    assert report.ok


def test_success_metric_missing_required_subkey_rejected() -> None:
    meta = _minimal_meta()
    meta["success_metric"] = {"name": "x", "current": 1, "target": 2}  # window_days missing
    report = validate_ticket_metadata(meta)
    assert not report.ok


def test_success_metric_window_days_zero_rejected() -> None:
    meta = _minimal_meta()
    meta["success_metric"] = {"name": "x", "current": 1, "target": 2, "window_days": 0}
    report = validate_ticket_metadata(meta)
    assert not report.ok


def test_success_metric_accepts_well_formed_object() -> None:
    meta = _minimal_meta()
    meta["success_metric"] = {"name": "x", "current": 1, "target": 2, "window_days": 7}
    report = validate_ticket_metadata(meta)
    assert report.ok


# ---------------------------------------------------------------------------
# Owner / acceptance_criteria / evidence
# ---------------------------------------------------------------------------


def test_owner_accepts_string() -> None:
    meta = _minimal_meta()
    meta["owner"] = "alice"
    report = validate_ticket_metadata(meta)
    assert report.ok


def test_owner_accepts_null() -> None:
    meta = _minimal_meta()
    meta["owner"] = None
    report = validate_ticket_metadata(meta)
    assert report.ok


def test_owner_rejects_integer() -> None:
    meta = _minimal_meta()
    meta["owner"] = 7
    report = validate_ticket_metadata(meta)
    assert not report.ok


def test_acceptance_criteria_empty_array_rejected() -> None:
    meta = _minimal_meta()
    meta["acceptance_criteria"] = []
    report = validate_ticket_metadata(meta)
    assert not report.ok


def test_acceptance_criteria_with_blank_item_rejected() -> None:
    meta = _minimal_meta()
    meta["acceptance_criteria"] = [""]
    report = validate_ticket_metadata(meta)
    assert not report.ok


def test_evidence_item_requires_source() -> None:
    meta = _minimal_meta()
    meta["evidence"] = [{"rows_cited": 5}]
    report = validate_ticket_metadata(meta)
    assert not report.ok


# ---------------------------------------------------------------------------
# Recommended-key warnings + strict mode promotion
# ---------------------------------------------------------------------------


def test_minimal_ticket_emits_recommended_warnings() -> None:
    report = validate_ticket_metadata(_minimal_meta(), strict=False)
    assert report.ok
    warning_paths = {tuple(w.path) for w in report.warnings}
    for key in RECOMMENDED_KEYS:
        assert (key,) in warning_paths


def test_strict_mode_promotes_warnings_to_errors() -> None:
    report = validate_ticket_metadata(_minimal_meta(), strict=True)
    assert not report.ok
    assert not report.warnings
    err_paths = {tuple(e.path) for e in report.errors}
    for key in RECOMMENDED_KEYS:
        assert (key,) in err_paths


def test_full_ticket_no_warnings_no_errors() -> None:
    meta = _minimal_meta()
    meta["owner"] = "alice"
    meta["success_metric"] = {
        "name": "lint_pass",
        "current": 0.5,
        "target": 0.9,
        "window_days": 7,
    }
    meta["acceptance_criteria"] = ["c1", "c2"]
    meta["evidence"] = [{"source": "audit.md", "rows_cited": 1, "value": "ok"}]
    meta["risk"] = "low"
    meta["rice"] = {"reach": 1, "impact": 1, "confidence": 0.7, "effort_days": 1, "score": 1}
    meta["ladder_to"] = "trust"
    report = validate_ticket_metadata(meta, strict=False)
    assert report.ok
    assert not report.warnings


# ---------------------------------------------------------------------------
# Report dataclass
# ---------------------------------------------------------------------------


def test_validation_report_to_dict_round_trip() -> None:
    report = validate_ticket_metadata({"id": "x"}, strict=False, path=Path("foo.md"))
    payload = report.to_dict()
    assert payload["path"] == "foo.md"
    assert payload["status"] in {"fail", "warn", "ok"}
    assert isinstance(payload["errors"], list)
    assert isinstance(payload["warnings"], list)


def test_validation_issue_render_path_present() -> None:
    issue = ValidationIssue(message="must be string", path=("rice", "score"), code="type")
    assert issue.render().startswith("rice.score:")


def test_validation_issue_render_path_empty() -> None:
    issue = ValidationIssue(message="top-level fail")
    assert issue.render() == "top-level fail"


def test_validation_report_error_factory() -> None:
    r = ValidationReport.error(Path("x.md"), "no frontmatter")
    assert r.status == "fail"
    assert r.errors[0].code == "parse_error"


def test_metadata_none_returns_error_report() -> None:
    r = validate_ticket_metadata(None)
    assert not r.ok
    assert "frontmatter" in r.errors[0].message


def test_metadata_non_mapping_returns_error_report() -> None:
    r = validate_ticket_metadata([1, 2, 3])  # type: ignore[arg-type]
    assert not r.ok


# ---------------------------------------------------------------------------
# File-on-disk paths
# ---------------------------------------------------------------------------


def test_validate_ticket_missing_file(tmp_path: Path) -> None:
    r = validate_ticket(tmp_path / "nope.md")
    assert not r.ok
    assert r.errors[0].code == "missing_file"


def test_validate_ticket_directory(tmp_path: Path) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()
    r = validate_ticket(sub)
    assert not r.ok
    assert r.errors[0].code == "not_a_file"


def test_validate_ticket_empty_file_reports_no_frontmatter(tmp_path: Path) -> None:
    p = tmp_path / "empty.md"
    p.write_text("")
    r = validate_ticket(p)
    assert not r.ok
    assert r.errors[0].code == "no_frontmatter"


def test_validate_ticket_no_frontmatter_reports_error() -> None:
    r = validate_ticket(FIXTURES / "invalid" / "no_frontmatter.md")
    assert not r.ok
    assert r.errors[0].code == "no_frontmatter"


@pytest.mark.parametrize(
    "filename",
    ["minimal.md", "rich.md", "yaml_only.yaml"],
)
def test_valid_fixtures_pass(filename: str) -> None:
    r = validate_ticket(FIXTURES / "valid" / filename)
    assert r.ok, f"{filename} should be valid, got errors: {[e.render() for e in r.errors]}"


def test_rich_fixture_has_no_warnings() -> None:
    r = validate_ticket(FIXTURES / "valid" / "rich.md")
    assert r.ok
    assert not r.warnings


@pytest.mark.parametrize(
    "filename",
    [
        "missing_priority.md",
        "bad_status.md",
        "bad_id.md",
        "bad_date.md",
        "no_frontmatter.md",
        "empty.md",
    ],
)
def test_invalid_fixtures_fail(filename: str) -> None:
    r = validate_ticket(FIXTURES / "invalid" / filename)
    assert not r.ok


def test_validate_ticket_strict_mode_on_minimal_fixture() -> None:
    r = validate_ticket(FIXTURES / "valid" / "minimal.md", strict=True)
    assert not r.ok  # minimal lacks recommended keys


def test_validate_ticket_strict_mode_on_rich_fixture_passes() -> None:
    r = validate_ticket(FIXTURES / "valid" / "rich.md", strict=True)
    assert r.ok


def test_validate_ticket_unknown_schema_raises_lookup() -> None:
    with pytest.raises(SchemaNotFoundError):
        validate_ticket(FIXTURES / "valid" / "minimal.md", schema_version="v123")


def test_validate_ticket_metadata_preserves_path_field() -> None:
    p = Path("/tmp/x.md")
    r = validate_ticket_metadata(_minimal_meta(), path=p)
    assert r.path == p


def test_validate_ticket_metadata_string_path_coerced() -> None:
    r = validate_ticket(str(FIXTURES / "valid" / "minimal.md"))
    assert r.ok


def test_deep_copy_of_metadata_is_not_mutated() -> None:
    meta = _minimal_meta()
    original = deepcopy(meta)
    validate_ticket_metadata(meta)
    assert meta == original


# ---------------------------------------------------------------------------
# Smoke / regression
# ---------------------------------------------------------------------------


def test_status_enum_includes_closed_hit() -> None:
    meta = _minimal_meta()
    meta["status"] = "closed_hit"
    r = validate_ticket_metadata(meta)
    assert r.ok


def test_status_enum_includes_superseded() -> None:
    meta = _minimal_meta()
    meta["status"] = "superseded"
    r = validate_ticket_metadata(meta)
    assert r.ok


def test_extra_unknown_keys_allowed() -> None:
    meta = _minimal_meta()
    meta["some_future_key"] = "anything"
    meta["another"] = {"nested": True}
    r = validate_ticket_metadata(meta)
    assert r.ok


def test_report_status_label_ordering() -> None:
    r_ok = validate_ticket_metadata(
        _minimal_meta()
        | {
            "owner": "x",
            "success_metric": {"name": "x", "current": 1, "target": 2, "window_days": 1},
            "acceptance_criteria": ["a"],
            "evidence": [{"source": "s"}],
            "risk": "low",
            "rice": {},
            "ladder_to": "x",
        }
    )
    assert r_ok.status == "ok"

    r_warn = validate_ticket_metadata(_minimal_meta())
    assert r_warn.status == "warn"

    bad = _minimal_meta()
    bad["status"] = "wip"
    r_fail = validate_ticket_metadata(bad)
    assert r_fail.status == "fail"
