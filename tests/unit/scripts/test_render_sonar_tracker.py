"""Unit tests for ``scripts/render_sonar_tracker.py``.

Covers the contract documented in docs/operations/sonar-tracker.md:

- grouping findings by severity (deterministic order)
- the 65536-char body cap (5000 synthetic findings -> body fits, no
  mid-line truncation)
- idempotency marker detection (find existing tracker issue)
- the trailing JSON summary block shape
- the empty-result path
- create-vs-update sync routing through a fake gh runner

The Sonar HTTP layer is exercised with respx so the pagination + quality
gate + coverage fetches are covered end to end without a live server.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
import respx

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "render_sonar_tracker.py"
HOST = "https://sonar.test"
PROJECT = "bernstein"


@pytest.fixture
def tracker() -> ModuleType:
    """Load scripts/render_sonar_tracker.py as a module."""
    spec = importlib.util.spec_from_file_location("render_sonar_tracker_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _issue(
    key: str,
    *,
    rule: str = "python:S3776",
    severity: str = "CRITICAL",
    issue_type: str = "CODE_SMELL",
    component: str = "bernstein:src/bernstein/cli/main.py",
    line: int | None = 10,
) -> dict[str, Any]:
    return {
        "key": key,
        "rule": rule,
        "severity": severity,
        "type": issue_type,
        "component": component,
        "line": line,
        "message": "vendor message that must never be rendered",
        "creationDate": "2026-05-20T10:00:00+0000",
    }


def _hotspot(
    key: str,
    *,
    rule_key: str = "python:S5042",
    component: str = "bernstein:src/bernstein/core/persistence/disaster_recovery.py",
    line: int | None = 349,
    status: str = "TO_REVIEW",
    security_category: str = "command-injection",
    vulnerability_probability: str = "HIGH",
) -> dict[str, Any]:
    return {
        "key": key,
        "ruleKey": rule_key,
        "component": component,
        "line": line,
        "status": status,
        "securityCategory": security_category,
        "vulnerabilityProbability": vulnerability_probability,
        "message": "vendor hotspot message that must never be rendered",
    }


def _snapshot(
    tracker: ModuleType,
    findings: list[dict[str, Any]],
    *,
    quality_gate: str = "ERROR",
    coverage: float | None = 19.3,
    quality_gate_conditions: list[Any] | None = None,
    security_hotspots: list[Any] | None = None,
) -> Any:
    normalised = [tracker._normalise_issue(raw) for raw in findings]
    return tracker.SonarSnapshot(
        findings=[f for f in normalised if f is not None],
        quality_gate=quality_gate,
        coverage=coverage,
        host=HOST,
        project_key=PROJECT,
        quality_gate_conditions=quality_gate_conditions or [],
        security_hotspots=security_hotspots or [],
    )


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------


def test_group_by_severity_buckets_and_orders(tracker: ModuleType) -> None:
    findings = [
        tracker._normalise_issue(_issue("c1", severity="CRITICAL", component="bernstein:z.py", line=9)),
        tracker._normalise_issue(_issue("b1", severity="BLOCKER")),
        tracker._normalise_issue(_issue("c2", severity="CRITICAL", component="bernstein:a.py", line=3)),
        tracker._normalise_issue(_issue("m1", severity="MAJOR")),
    ]
    buckets = tracker.group_by_severity([f for f in findings if f is not None])
    assert [f.key for f in buckets["BLOCKER"]] == ["b1"]
    # CRITICAL sorted by component path then line: a.py before z.py.
    assert [f.key for f in buckets["CRITICAL"]] == ["c2", "c1"]
    assert [f.key for f in buckets["MAJOR"]] == ["m1"]
    # Empty buckets present for the remaining canonical severities.
    assert buckets["MINOR"] == []
    assert buckets["INFO"] == []


def test_group_by_severity_handles_unexpected_label(tracker: ModuleType) -> None:
    findings = [tracker._normalise_issue(_issue("x1", severity="WEIRD"))]
    buckets = tracker.group_by_severity([f for f in findings if f is not None])
    assert "WEIRD" in buckets
    assert [f.key for f in buckets["WEIRD"]] == ["x1"]


# ---------------------------------------------------------------------------
# TL;DR + JSON summary block shape
# ---------------------------------------------------------------------------


def test_body_tldr_table_shows_gate_and_coverage(tracker: ModuleType) -> None:
    snapshot = _snapshot(tracker, [_issue("b1", severity="BLOCKER")], quality_gate="ERROR", coverage=19.3)
    body = tracker.render_body(snapshot, generated_at="2026-05-21T00:00:00+00:00")
    assert "Quality gate: **ERROR**" in body
    assert "Coverage: **19.3%**" in body
    assert tracker.TRACKER_MARKER in body
    assert "| BLOCKER | 1 |" in body


def test_json_summary_block_shape(tracker: ModuleType) -> None:
    findings = [
        _issue("b1", severity="BLOCKER"),
        _issue("c1", severity="CRITICAL"),
        _issue("c2", severity="CRITICAL"),
        _issue("m1", severity="MAJOR"),
    ]
    snapshot = _snapshot(tracker, findings, quality_gate="ERROR", coverage=19.3)
    body = tracker.render_body(snapshot, generated_at="2026-05-21T00:00:00+00:00")
    # Extract the fenced json block.
    start = body.index("```json") + len("```json")
    end = body.index("```", start)
    parsed = json.loads(body[start:end])
    assert parsed["generated_at"] == "2026-05-21T00:00:00+00:00"
    assert parsed["quality_gate"] == "ERROR"
    assert parsed["coverage"] == 19.3
    assert parsed["by_severity"] == {"BLOCKER": 1, "CRITICAL": 2, "MAJOR": 1}
    assert parsed["blocker_keys"] == ["b1"]
    assert sorted(parsed["critical_keys"]) == ["c1", "c2"]


def test_body_renders_quality_gate_conditions(tracker: ModuleType) -> None:
    condition = tracker.QualityGateCondition(
        metric_key="branch_coverage",
        status="ERROR",
        comparator="LT",
        error_threshold="80",
        actual_value="71.2",
    )
    snapshot = _snapshot(
        tracker,
        [],
        quality_gate="ERROR",
        coverage=80.1,
        quality_gate_conditions=[condition],
    )

    body = tracker.render_body(snapshot, generated_at="2026-05-21T00:00:00+00:00")

    assert "## Quality Gate Conditions" in body
    assert "| Metric | Status | Actual | Comparator | Threshold |" in body
    assert "| `branch_coverage` | ERROR | 71.2 | LT | 80 |" in body
    start = body.index("```json") + len("```json")
    end = body.index("```", start)
    parsed = json.loads(body[start:end])
    assert parsed["quality_gate_conditions"] == [
        {
            "actual_value": "71.2",
            "comparator": "LT",
            "error_threshold": "80",
            "metric_key": "branch_coverage",
            "status": "ERROR",
        }
    ]


def test_body_renders_security_hotspots_without_vendor_message(tracker: ModuleType) -> None:
    hotspot = tracker.SecurityHotspot(
        key="HS-1",
        rule_key="python:S5042",
        component="bernstein:src/bernstein/core/persistence/disaster_recovery.py",
        line=349,
        status="TO_REVIEW",
        security_category="command-injection",
        vulnerability_probability="HIGH",
    )
    snapshot = _snapshot(tracker, [], security_hotspots=[hotspot])

    body = tracker.render_body(snapshot, generated_at="2026-05-21T00:00:00+00:00")

    assert "## Security Hotspots To Review" in body
    assert "| `python:S5042` | TO_REVIEW | command-injection | HIGH |" in body
    assert "`src/bernstein/core/persistence/disaster_recovery.py:349`" in body
    assert f"{HOST}/security_hotspots?id={PROJECT}&hotspots=HS-1" in body
    assert "vendor hotspot message" not in body
    start = body.index("```json") + len("```json")
    end = body.index("```", start)
    parsed = json.loads(body[start:end])
    assert parsed["security_hotspots"] == [
        {
            "component": "bernstein:src/bernstein/core/persistence/disaster_recovery.py",
            "key": "HS-1",
            "line": 349,
            "rule_key": "python:S5042",
            "security_category": "command-injection",
            "status": "TO_REVIEW",
            "vulnerability_probability": "HIGH",
        }
    ]


def test_md_inline_escapes_pipe_and_backtick(tracker: ModuleType) -> None:
    # A pipe would break the surrounding table column; a backtick would
    # terminate a code span. Both are neutralised.
    assert tracker._md_inline("a|b") == "a\\|b"
    assert tracker._md_inline("we`ird") == "we'ird"
    assert tracker._md_inline("py:S1|`x`") == "py:S1\\|'x'"


def test_finding_line_escapes_pipe_and_backtick_in_rule_and_path(tracker: ModuleType) -> None:
    # Craft a rule key and component path carrying markdown-hostile bytes.
    snapshot = _snapshot(
        tracker,
        [
            _issue(
                "b1",
                severity="BLOCKER",
                rule="python:S20|68",
                component="bernstein:src/we`ird|path.py",
            )
        ],
    )
    body = tracker.render_body(snapshot)
    # The trailing JSON summary legitimately carries raw values (they are
    # JSON-escaped, not markdown), so scope the check to the markdown part.
    markdown = body.split("```json")[0]
    # Inside a backtick code span the raw pipe/backtick must not survive
    # (they would terminate the span / break a table); the escaped forms
    # appear instead. safe_why prose in the list item may still mention
    # the rule key in plain text, which is harmless outside a span/table.
    assert "`python:S20\\|68`" in markdown
    assert "`python:S20|68`" not in markdown
    assert "`src/we'ird\\|path.py:10`" in markdown
    assert "`src/we`ird|path.py:10`" not in markdown


def test_quality_gate_condition_escapes_metric_key(tracker: ModuleType) -> None:
    condition = tracker.QualityGateCondition(
        metric_key="new_cov|erage",
        status="ERROR",
        comparator="LT",
        error_threshold="80",
        actual_value="71.2",
    )
    snapshot = _snapshot(tracker, [], quality_gate_conditions=[condition])
    markdown = tracker.render_body(snapshot).split("```json")[0]
    assert "| `new_cov\\|erage` |" in markdown
    assert "new_cov|erage" not in markdown


def test_hotspot_escapes_rule_key_and_location(tracker: ModuleType) -> None:
    hotspot = tracker.SecurityHotspot(
        key="HS-1",
        rule_key="python:S50|42",
        component="bernstein:src/we`ird.py",
        line=7,
        status="TO_REVIEW",
        security_category="command-injection",
        vulnerability_probability="HIGH",
    )
    snapshot = _snapshot(tracker, [], security_hotspots=[hotspot])
    markdown = tracker.render_body(snapshot).split("```json")[0]
    assert "python:S50\\|42" in markdown
    assert "S50|42" not in markdown
    assert "src/we'ird.py:7" in markdown


def test_blocker_and_critical_rendered_as_checkboxes_with_permalink(tracker: ModuleType) -> None:
    snapshot = _snapshot(tracker, [_issue("b1", severity="BLOCKER", rule="python:S2068")])
    body = tracker.render_body(snapshot)
    assert "- [ ] rule `python:S2068`" in body
    assert "open=b1" in body
    assert f"{HOST}/project/issues?issueStatuses=OPEN,CONFIRMED&id={PROJECT}&open=b1" in body


# ---------------------------------------------------------------------------
# Empty-result path
# ---------------------------------------------------------------------------


def test_empty_result_renders_zero_total(tracker: ModuleType) -> None:
    snapshot = _snapshot(tracker, [], quality_gate="OK", coverage=88.0)
    body = tracker.render_body(snapshot)
    assert "Open findings: **0**" in body
    assert "| **Total** | **0** |" in body
    assert "Quality gate: **OK**" in body
    # JSON block still valid with empty key lists.
    start = body.index("```json") + len("```json")
    end = body.index("```", start)
    parsed = json.loads(body[start:end])
    assert parsed["blocker_keys"] == []
    assert parsed["critical_keys"] == []
    assert parsed["by_severity"] == {}


# ---------------------------------------------------------------------------
# 65k char cap with no mid-line truncation
# ---------------------------------------------------------------------------


def _synthetic_findings(n: int) -> list[dict[str, Any]]:
    """Build *n* findings spread across severities with long components."""
    severities = ["BLOCKER", "CRITICAL", "MAJOR", "MINOR", "INFO"]
    out: list[dict[str, Any]] = []
    for i in range(n):
        sev = severities[i % len(severities)]
        comp = f"bernstein:src/bernstein/some/deeply/nested/module_{i:05d}/file_{i:05d}.py"
        out.append(_issue(f"KEY-{i:06d}", severity=sev, component=comp, line=i % 900 + 1))
    return out


def test_body_respects_github_limit_with_5000_findings(tracker: ModuleType) -> None:
    snapshot = _snapshot(tracker, _synthetic_findings(5000), quality_gate="ERROR", coverage=19.3)
    body = tracker.render_body(snapshot)
    assert len(body) <= tracker.GITHUB_BODY_LIMIT
    # No mid-line truncation: every line is whole. The body ends with the
    # closing fence of the JSON block, and the JSON parses.
    assert body.endswith("```\n")
    start = body.index("```json") + len("```json")
    end = body.index("```", start)
    parsed = json.loads(body[start:end])
    # All 5000 findings are still counted in the summary even though the
    # per-severity item lists were collapsed to fit.
    assert sum(parsed["by_severity"].values()) == 5000


def test_json_summary_caps_key_lists_and_records_truncation(tracker: ModuleType) -> None:
    # More BLOCKER + CRITICAL findings than the JSON key cap forces the key
    # lists to be truncated, with a sibling count of how many were dropped.
    cap = tracker._JSON_KEYS_CAP
    findings = [_issue(f"B-{i:05d}", severity="BLOCKER", component=f"bernstein:b{i}.py") for i in range(cap + 25)]
    findings += [_issue(f"C-{i:05d}", severity="CRITICAL", component=f"bernstein:c{i}.py") for i in range(cap + 10)]
    snapshot = _snapshot(tracker, findings)
    body = tracker.render_body(snapshot)
    start = body.index("```json") + len("```json")
    end = body.index("```", start)
    parsed = json.loads(body[start:end])
    assert len(parsed["blocker_keys"]) == cap
    assert len(parsed["critical_keys"]) == cap
    assert parsed["blocker_keys_truncated"] == 25
    assert parsed["critical_keys_truncated"] == 10
    # Counts stay honest even though the key lists are capped.
    assert parsed["by_severity"]["BLOCKER"] == cap + 25
    assert parsed["by_severity"]["CRITICAL"] == cap + 10


def test_json_summary_omits_truncation_field_when_under_cap(tracker: ModuleType) -> None:
    snapshot = _snapshot(tracker, [_issue("b1", severity="BLOCKER"), _issue("c1", severity="CRITICAL")])
    body = tracker.render_body(snapshot)
    start = body.index("```json") + len("```json")
    end = body.index("```", start)
    parsed = json.loads(body[start:end])
    assert "blocker_keys_truncated" not in parsed
    assert "critical_keys_truncated" not in parsed


def test_body_no_partial_list_item_when_collapsed(tracker: ModuleType) -> None:
    # 5000 findings forces collapse; assert no line is a dangling fragment
    # (every list bullet line ends with the closing of a markdown link or
    # is a plain count line).
    snapshot = _snapshot(tracker, _synthetic_findings(5000))
    body = tracker.render_body(snapshot)
    for line in body.splitlines():
        if line.startswith("- [ ] ") or line.startswith("- rule "):
            # A rendered finding line always ends with the closing paren of
            # its ([view](...)) permalink.
            assert line.rstrip().endswith(")"), line


def test_item_cap_shrinks_before_sections_collapse_to_counts(tracker: ModuleType) -> None:
    # Three large <details> severities with very long component paths push the
    # body over budget. The renderer must first shrink the per-section item
    # cap (keeping the sections as <details>) before it drops a whole section
    # to a counts-only line. Assert the sections survive with fewer items.
    pad = "z" * 1000
    findings: list[dict[str, Any]] = []
    for sev in ("MAJOR", "MINOR", "INFO"):
        for i in range(200):
            comp = f"bernstein:src/{pad}/m_{i:05d}/file_{i:05d}.py"
            findings.append(_issue(f"{sev[0]}-{i:05d}", severity=sev, component=comp, line=i % 900 + 1))
    snapshot = _snapshot(tracker, findings)
    body = tracker.render_body(snapshot)
    assert len(body) <= tracker.GITHUB_BODY_LIMIT
    # All three sections are still rendered as <details>, not collapsed to a
    # counts-only line. (The item cap was halved instead.)
    assert body.count("<details>") == 3
    for sev in ("MAJOR", "MINOR", "INFO"):
        assert f"<summary>{sev} (200)</summary>" in body
        assert f"- **{sev}**:" not in body
    # Fewer than the default cap of items are shown per section.
    rendered = sum(1 for line in body.splitlines() if line.startswith("- rule "))
    assert 0 < rendered < 3 * tracker._DETAILS_ITEM_CAP


def test_small_input_lists_everything_in_full(tracker: ModuleType) -> None:
    snapshot = _snapshot(tracker, _synthetic_findings(20))
    body = tracker.render_body(snapshot)
    # No collapse needed: BLOCKER + CRITICAL are full checkbox lists and the
    # body is comfortably under the cap.
    assert len(body) < tracker.GITHUB_BODY_LIMIT
    assert "## BLOCKER" in body
    assert "## CRITICAL" in body
    assert "- [ ] rule" in body


# ---------------------------------------------------------------------------
# Idempotency marker detection + sync routing (fake gh runner)
# ---------------------------------------------------------------------------


class _FakeGh:
    """Records gh invocations and returns scripted responses."""

    def __init__(
        self, *, list_payload: list[dict[str, Any]], create_url: str = f"https://github.com/{PROJECT}/issues/4242"
    ):
        self._list_payload = list_payload
        self._create_url = create_url
        self.calls: list[list[str]] = []

    def __call__(
        self,
        cmd: list[str],
        *,
        capture_output: bool = False,
        text: bool = False,
        check: bool = False,
        input: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        sub = cmd[1] if len(cmd) > 1 else ""
        action = cmd[2] if len(cmd) > 2 else ""
        if sub == "label":
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if sub == "issue" and action == "list":
            return subprocess.CompletedProcess(cmd, 0, json.dumps(self._list_payload), "")
        if sub == "issue" and action == "create":
            return subprocess.CompletedProcess(cmd, 0, f"{self._create_url}\n", "")
        if sub == "issue" and action == "edit":
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")


def test_find_tracker_issue_matches_marker(tracker: ModuleType) -> None:
    fake = _FakeGh(
        list_payload=[
            {"number": 7, "body": "some unrelated tracker without the marker"},
            {"number": 9, "body": f"header\n{tracker.TRACKER_MARKER}\nbody"},
        ]
    )
    number = tracker.find_tracker_issue(PROJECT, runner=fake)
    assert number == 9


def test_find_tracker_issue_returns_none_when_no_marker(tracker: ModuleType) -> None:
    fake = _FakeGh(list_payload=[{"number": 7, "body": "no marker here"}])
    assert tracker.find_tracker_issue(PROJECT, runner=fake) is None


def test_sync_creates_when_absent(tracker: ModuleType) -> None:
    fake = _FakeGh(list_payload=[])
    number, action = tracker.sync_issue("body text", PROJECT, runner=fake)
    assert action == "created"
    assert number == 4242
    # A create call was issued with the title + labels.
    create_calls = [c for c in fake.calls if c[1:3] == ["issue", "create"]]
    assert create_calls, fake.calls
    assert tracker.TRACKER_TITLE in create_calls[0]


def test_sync_updates_when_marker_present(tracker: ModuleType) -> None:
    fake = _FakeGh(
        list_payload=[{"number": 55, "body": f"x\n{tracker.TRACKER_MARKER}\ny"}],
    )
    number, action = tracker.sync_issue("new body", PROJECT, runner=fake)
    assert action == "updated"
    assert number == 55
    edit_calls = [c for c in fake.calls if c[1:3] == ["issue", "edit"]]
    assert edit_calls, fake.calls
    assert "55" in edit_calls[0]
    # No create call when an existing issue was found.
    assert not [c for c in fake.calls if c[1:3] == ["issue", "create"]]


def test_find_tracker_issue_raises_on_gh_failure(tracker: ModuleType) -> None:
    def _boom(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 1, "", "boom")

    with pytest.raises(tracker.GitHubSyncError):
        tracker.find_tracker_issue(PROJECT, runner=_boom)


# ---------------------------------------------------------------------------
# Sonar HTTP layer (respx): pagination + quality gate + coverage
# ---------------------------------------------------------------------------


def test_collect_snapshot_paginates_and_fetches_gate_coverage(tracker: ModuleType) -> None:
    config = tracker.SonarConfig(host=HOST, token="t0ken", project_key=PROJECT)

    page1 = {
        "issues": [_issue(f"P1-{i}", severity="MAJOR") for i in range(500)],
        "paging": {"pageIndex": 1, "pageSize": 500, "total": 750},
    }
    page2 = {
        "issues": [_issue(f"P2-{i}", severity="MINOR") for i in range(250)],
        "paging": {"pageIndex": 2, "pageSize": 500, "total": 750},
    }

    def _issues_responder(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("p")
        return httpx.Response(200, json=page1 if page == "1" else page2)

    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{HOST}/api/issues/search").mock(side_effect=_issues_responder)
        mock.get(f"{HOST}/api/qualitygates/project_status").mock(
            return_value=httpx.Response(
                200,
                json={
                    "projectStatus": {
                        "status": "ERROR",
                        "conditions": [
                            {
                                "metricKey": "branch_coverage",
                                "status": "ERROR",
                                "comparator": "LT",
                                "errorThreshold": "80",
                                "actualValue": "71.2",
                            }
                        ],
                    }
                },
            )
        )
        mock.get(f"{HOST}/api/measures/component").mock(
            return_value=httpx.Response(
                200,
                json={"component": {"measures": [{"metric": "coverage", "value": "19.3"}]}},
            )
        )
        mock.get(f"{HOST}/api/hotspots/search").mock(
            return_value=httpx.Response(
                200,
                json={
                    "hotspots": [_hotspot("HS-1")],
                    "paging": {"pageIndex": 1, "pageSize": 500, "total": 1},
                },
            )
        )
        snapshot = tracker.collect_snapshot(config)

    assert len(snapshot.findings) == 750
    assert snapshot.quality_gate == "ERROR"
    assert snapshot.quality_gate_conditions == [
        tracker.QualityGateCondition(
            metric_key="branch_coverage",
            status="ERROR",
            comparator="LT",
            error_threshold="80",
            actual_value="71.2",
        )
    ]
    assert snapshot.coverage is not None
    assert snapshot.coverage == pytest.approx(19.3, abs=0.001)  # pyright: ignore[reportUnknownMemberType]
    assert snapshot.security_hotspots == [
        tracker.SecurityHotspot(
            key="HS-1",
            rule_key="python:S5042",
            component="bernstein:src/bernstein/core/persistence/disaster_recovery.py",
            line=349,
            status="TO_REVIEW",
            security_category="command-injection",
            vulnerability_probability="HIGH",
        )
    ]


def test_fetch_all_findings_raises_on_non_object_payload(tracker: ModuleType) -> None:
    # A malformed page (a JSON array instead of an object) must fail closed so
    # an incomplete finding set is never published as a successful run.
    config = tracker.SonarConfig(host=HOST, token="t", project_key=PROJECT)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{HOST}/api/issues/search").mock(return_value=httpx.Response(200, json=[1, 2, 3]))
        with pytest.raises(tracker.SonarAPIError):
            tracker.fetch_all_findings(config)


def test_fetch_all_findings_raises_on_bad_paging(tracker: ModuleType) -> None:
    config = tracker.SonarConfig(host=HOST, token="t", project_key=PROJECT)
    bad = {"issues": [_issue("b1", severity="BLOCKER")], "paging": {"total": "not-a-number"}}
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{HOST}/api/issues/search").mock(return_value=httpx.Response(200, json=bad))
        with pytest.raises(tracker.SonarAPIError):
            tracker.fetch_all_findings(config)


def test_fetch_quality_gate_unknown_on_bad_payload(tracker: ModuleType) -> None:
    config = tracker.SonarConfig(host=HOST, token="t", project_key=PROJECT)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{HOST}/api/qualitygates/project_status").mock(return_value=httpx.Response(200, json={"nope": 1}))
        assert tracker.fetch_quality_gate(config) == "UNKNOWN"


def test_fetch_coverage_none_when_metric_absent(tracker: ModuleType) -> None:
    config = tracker.SonarConfig(host=HOST, token="t", project_key=PROJECT)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{HOST}/api/measures/component").mock(
            return_value=httpx.Response(200, json={"component": {"measures": []}})
        )
        assert tracker.fetch_coverage(config) is None


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def test_cli_dry_run_with_fixture(tracker: ModuleType, tmp_path: Path) -> None:
    fixture = tmp_path / "issues.json"
    fixture.write_text(
        json.dumps(
            {
                "issues": [_issue("b1", severity="BLOCKER")],
                "quality_gate": "ERROR",
                "coverage": 19.3,
                "host": HOST,
                "project_key": PROJECT,
            }
        ),
        encoding="utf-8",
    )
    out_body = tmp_path / "body.md"
    rc = tracker.main(["--dry-run", "--fixture", str(fixture), "--output-body", str(out_body)])
    assert rc == 0
    text = out_body.read_text(encoding="utf-8")
    assert tracker.TRACKER_MARKER in text
    assert "open=b1" in text
    # Raw vendor message is never rendered.
    assert "vendor message" not in text


def test_forbidden_guard_ignores_token_inside_identifier(tracker: ModuleType) -> None:
    """Guard must not reject ordinary identifiers that contain a token."""
    tracker._assert_no_forbidden("rule `python:S1172` in `src/bernstein/adapters/droid.py`")


def test_forbidden_guard_blocks_standalone_token(tracker: ModuleType) -> None:
    """Guard still blocks standalone disallowed terms."""
    with pytest.raises(AssertionError, match="roi"):
        tracker._assert_no_forbidden("remove ROI wording from the tracker")
