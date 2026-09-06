"""Tests for the operator-controlled source-to-sensitivity map (issue #5042).

Slice 2. The projection in :mod:`bernstein.core.lineage.sensitivity` reads
classifications off signed lineage entries; something has to put them there.
The map is the operator-controlled table that says which class a source's
results carry, mirroring ``load_trust_source_map`` on the opposite axis.

The fail-closed direction is inverted with the axis: an unlisted source is the
**highest** class, not the lowest.
"""

from __future__ import annotations

from pathlib import Path

from bernstein.core.lineage.sensitivity import (
    HIGHEST_SENSITIVITY_CLASS,
    SensitivityClass,
    load_sensitivity_source_map,
    sensitivity_class_for_source,
)


def _write_map(workdir: Path, body: str) -> None:
    d = workdir / "templates" / "provenance"
    d.mkdir(parents=True, exist_ok=True)
    (d / "sensitivity_sources.yaml").write_text(body, encoding="utf-8")


def test_bundled_sensitivity_source_map_classifies_the_reviewed_sources() -> None:
    mapping = load_sensitivity_source_map()
    # Operator-supplied material is classified; public web is not.
    assert mapping["operator.attachment"] is SensitivityClass.CONFIDENTIAL
    assert mapping["web.fetch"] is SensitivityClass.PUBLIC
    # Repository state is company-internal by default.
    assert mapping["fs.read"] is SensitivityClass.INTERNAL
    # Every value is a real class, never a raw string.
    assert all(isinstance(v, SensitivityClass) for v in mapping.values())


def test_unknown_source_fails_closed_to_the_highest_class() -> None:
    mapping = {"web.fetch": SensitivityClass.PUBLIC}
    assert sensitivity_class_for_source("totally.unknown.tool", mapping) is HIGHEST_SENSITIVITY_CLASS
    assert sensitivity_class_for_source("totally.unknown.tool", mapping) is SensitivityClass.RESTRICTED
    # A listed source still answers with what the operator recorded.
    assert sensitivity_class_for_source("web.fetch", mapping) is SensitivityClass.PUBLIC


def test_default_map_is_loaded_when_no_mapping_is_passed() -> None:
    assert sensitivity_class_for_source("operator.attachment") is SensitivityClass.CONFIDENTIAL


def test_workdir_map_overrides_the_bundled_default(tmp_path: Path) -> None:
    # An operator's own classification of a source wins over the shipped one.
    _write_map(tmp_path, "sources:\n  - name: web.fetch\n    sensitivity: restricted\n")
    mapping = load_sensitivity_source_map(workdir=tmp_path)
    assert mapping["web.fetch"] is SensitivityClass.RESTRICTED
    # The local file replaces the bundled table rather than merging into it,
    # so a source the operator did not list is unlisted -- and unlisted is
    # fail-closed-high, never a silently inherited default.
    assert "operator.attachment" not in mapping
    assert sensitivity_class_for_source("operator.attachment", mapping) is SensitivityClass.RESTRICTED


def test_malformed_rows_are_dropped_without_dropping_the_whole_map(tmp_path: Path) -> None:
    _write_map(
        tmp_path,
        "sources:\n"
        "  - name: good.source\n"
        "    sensitivity: internal\n"
        "  - not-a-mapping\n"
        "  - name: ''\n"
        "    sensitivity: internal\n"
        "  - sensitivity: internal\n",
    )
    mapping = load_sensitivity_source_map(workdir=tmp_path)
    assert mapping == {"good.source": SensitivityClass.INTERNAL}


def test_unrecognised_class_token_is_dropped_rather_than_downgraded(tmp_path: Path) -> None:
    # A typo must not quietly become the least sensitive class. The row is
    # dropped, so the source reads as unlisted and fails closed to the highest.
    _write_map(
        tmp_path,
        "sources:\n  - name: typo.source\n    sensitivity: confidental\n  - name: ok.source\n    sensitivity: public\n",
    )
    mapping = load_sensitivity_source_map(workdir=tmp_path)
    assert "typo.source" not in mapping
    assert mapping["ok.source"] is SensitivityClass.PUBLIC
    assert sensitivity_class_for_source("typo.source", mapping) is SensitivityClass.RESTRICTED


def test_class_tokens_are_read_case_and_whitespace_insensitively(tmp_path: Path) -> None:
    _write_map(tmp_path, "sources:\n  - name: '  spaced.source  '\n    sensitivity: '  Confidential  '\n")
    mapping = load_sensitivity_source_map(workdir=tmp_path)
    assert mapping == {"spaced.source": SensitivityClass.CONFIDENTIAL}


def test_unreadable_map_yields_an_empty_table_not_a_partial_one(tmp_path: Path) -> None:
    _write_map(tmp_path, "sources: [ this is not: valid: yaml\n")
    assert load_sensitivity_source_map(workdir=tmp_path) == {}


def test_map_without_a_sources_list_yields_an_empty_table(tmp_path: Path) -> None:
    _write_map(tmp_path, "sources: not-a-list\n")
    assert load_sensitivity_source_map(workdir=tmp_path) == {}
