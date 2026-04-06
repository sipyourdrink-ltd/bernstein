"""Tests for bernstein.core.config_diff_cmd (CFG-009)."""

from __future__ import annotations

from bernstein.core.config_diff_cmd import (
    ConfigDeviation,
    ConfigDiffReport,
    diff_against_defaults,
)


class TestConfigDeviation:
    def test_to_dict(self) -> None:
        d = ConfigDeviation(key="model", kind="changed", default_value=None, current_value="opus")
        result = d.to_dict()
        assert result["key"] == "model"
        assert result["kind"] == "changed"
        assert result["current_value"] == "opus"


class TestConfigDiffReport:
    def test_has_deviations_empty(self) -> None:
        report = ConfigDiffReport()
        assert not report.has_deviations

    def test_has_deviations_with_changes(self) -> None:
        report = ConfigDiffReport(
            deviations=[ConfigDeviation(key="x", kind="added", current_value=1)],
            added_count=1,
        )
        assert report.has_deviations

    def test_to_dict(self) -> None:
        report = ConfigDiffReport(total_keys=10, changed_count=2)
        d = report.to_dict()
        assert d["total_keys"] == 10
        assert d["changed_count"] == 2


class TestDiffAgainstDefaults:
    def test_no_changes(self) -> None:
        defaults = {"max_agents": 6, "cli": "auto"}
        current = {"max_agents": 6, "cli": "auto"}
        report = diff_against_defaults(current, defaults)
        assert not report.has_deviations
        assert report.changed_count == 0

    def test_changed_value(self) -> None:
        defaults = {"max_agents": 6}
        current = {"max_agents": 10}
        report = diff_against_defaults(current, defaults)
        assert report.changed_count == 1
        assert report.deviations[0].kind == "changed"
        assert report.deviations[0].default_value == 6
        assert report.deviations[0].current_value == 10

    def test_added_key(self) -> None:
        defaults = {"max_agents": 6}
        current = {"max_agents": 6, "custom_key": "value"}
        report = diff_against_defaults(current, defaults)
        assert report.added_count == 1
        assert report.deviations[0].kind == "added"

    def test_removed_key(self) -> None:
        defaults = {"max_agents": 6, "model": None}
        current = {"max_agents": 6}
        report = diff_against_defaults(current, defaults)
        assert report.removed_count == 1
        assert report.deviations[0].kind == "removed"

    def test_nested_config_flattened(self) -> None:
        defaults = {"quality_gates": {"enabled": True}}
        current = {"quality_gates": {"enabled": False}}
        report = diff_against_defaults(current, defaults)
        assert report.changed_count == 1
        assert "quality_gates.enabled" in report.deviations[0].key

    def test_uses_builtin_defaults(self) -> None:
        current = {"max_agents": 99, "cli": "auto"}
        report = diff_against_defaults(current)
        # max_agents default is 6, so 99 is a change.
        changed_keys = [d.key for d in report.deviations if d.kind == "changed"]
        assert "max_agents" in changed_keys

    def test_total_keys_correct(self) -> None:
        defaults = {"a": 1, "b": 2}
        current = {"b": 2, "c": 3}
        report = diff_against_defaults(current, defaults)
        assert report.total_keys == 3  # a, b, c
