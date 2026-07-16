"""Unit tests for the SARIF emitter in ``scripts/sweep_sonar_findings.py``.

Covers the ``--emit-sarif`` path that feeds GitHub code scanning:
required SARIF 2.1.0 structure, project-key prefix stripping, the
VULNERABILITY -> error + security-severity mapping, security-hotspot
handling, byte-for-byte determinism on re-run, and the security-scope
filter that keeps pure-maintainability CODE_SMELL findings out of the
Security tab.
"""

from __future__ import annotations

import json
from pathlib import Path

from sweep_sonar_findings import (  # type: ignore[import-not-found]
    SARIF_CODE_SCANNING_TYPES,
    Finding,
    build_sarif,
    filter_sarif_findings,
    main,
)
from sweep_sonar_findings import (  # type: ignore[import-not-found]
    _load_sarif_fixture as load_sarif_fixture,
)

_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
_ISSUES_FIXTURE = _FIXTURE_DIR / "issues_search.json"
_HOTSPOTS_FIXTURE = _FIXTURE_DIR / "sonar_sarif_hotspots.json"

_HOST = "https://sonar.example.com"


def _build_from(fixture: Path) -> dict:
    findings = load_sarif_fixture(fixture)
    return build_sarif(findings, host=_HOST)


def _rules_by_id(sarif: dict) -> dict:
    return {r["id"]: r for r in sarif["runs"][0]["tool"]["driver"]["rules"]}


def _result_by_fingerprint(sarif: dict, key: str) -> dict:
    for res in sarif["runs"][0]["results"]:
        if res["partialFingerprints"]["sonarFindingKey"] == key:
            return res
    raise AssertionError(f"no result with sonarFindingKey={key!r}")


# ---------------------------------------------------------------------------
# Required SARIF 2.1.0 structure
# ---------------------------------------------------------------------------


def test_sarif_required_structure() -> None:
    sarif = _build_from(_ISSUES_FIXTURE)
    assert sarif["$schema"].endswith("sarif-schema-2.1.0.json")
    assert sarif["version"] == "2.1.0"
    assert isinstance(sarif["runs"], list) and len(sarif["runs"]) == 1
    driver = sarif["runs"][0]["tool"]["driver"]
    assert driver["name"] == "SonarQube"
    assert driver["informationUri"] == _HOST
    results = sarif["runs"][0]["results"]
    assert results, "expected at least one result"
    for res in results:
        assert res["ruleId"]
        phys = res["locations"][0]["physicalLocation"]
        assert phys["artifactLocation"]["uri"]
        assert phys["region"]["startLine"] >= 1
        assert "text" in res["message"]
        assert res["partialFingerprints"]["sonarFindingKey"]


def test_rules_and_results_counts_match_fixture() -> None:
    sarif = _build_from(_ISSUES_FIXTURE)
    # The canonical fixture has 6 findings across 6 distinct rule keys.
    assert len(sarif["runs"][0]["results"]) == 6
    assert len(sarif["runs"][0]["tool"]["driver"]["rules"]) == 6


# ---------------------------------------------------------------------------
# Project-key prefix stripping
# ---------------------------------------------------------------------------


def test_strips_project_key_prefix() -> None:
    sarif = _build_from(_ISSUES_FIXTURE)
    uris = {res["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] for res in sarif["runs"][0]["results"]}
    # No SARIF uri retains the Sonar "bernstein:" component prefix.
    assert all(not uri.startswith("bernstein:") for uri in uris)
    assert "src/bernstein/core/agents/secrets_loader.py" in uris


def test_prefix_stripped_exact_path() -> None:
    sarif = _build_from(_HOTSPOTS_FIXTURE)
    res = _result_by_fingerprint(sarif, "ISSUE-VULN-001")
    uri = res["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
    assert uri == "src/bernstein/core/x.py"


def test_bare_path_component_left_intact() -> None:
    sarif = _build_from(_HOTSPOTS_FIXTURE)
    res = _result_by_fingerprint(sarif, "HOTSPOT-LOW-001")
    uri = res["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
    assert uri == "src/bernstein/core/rng.py"


# ---------------------------------------------------------------------------
# Severity / level mapping
# ---------------------------------------------------------------------------


def test_vulnerability_maps_to_error_with_security_severity() -> None:
    sarif = _build_from(_ISSUES_FIXTURE)
    # FINDING-BLOCKER-001 is a BLOCKER VULNERABILITY (rule python:S2068).
    res = _result_by_fingerprint(sarif, "FINDING-BLOCKER-001")
    assert res["level"] == "error"
    rule = _rules_by_id(sarif)["python:S2068"]
    assert rule["properties"]["security-severity"] == "9.5"


def test_non_security_rule_has_no_security_severity() -> None:
    sarif = _build_from(_ISSUES_FIXTURE)
    # python:S3776 is a CODE_SMELL; it must not be badged as a security alert.
    rule = _rules_by_id(sarif)["python:S3776"]
    assert "security-severity" not in rule["properties"]


def test_major_maps_to_warning_and_default_line() -> None:
    sarif = _build_from(_ISSUES_FIXTURE)
    major = _result_by_fingerprint(sarif, "FINDING-MAJOR-001")
    assert major["level"] == "warning"
    # FINDING-BUG-001 has a null line -> region.startLine defaults to 1.
    bug = _result_by_fingerprint(sarif, "FINDING-BUG-001")
    assert bug["locations"][0]["physicalLocation"]["region"]["startLine"] == 1


# ---------------------------------------------------------------------------
# Security hotspots + rule names
# ---------------------------------------------------------------------------


def test_hotspots_mapped_by_probability() -> None:
    sarif = _build_from(_HOTSPOTS_FIXTURE)
    rules = _rules_by_id(sarif)
    high = _result_by_fingerprint(sarif, "HOTSPOT-HIGH-001")
    assert high["level"] == "error"
    assert rules["python:S4830"]["properties"]["security-severity"] == "8.0"
    low = _result_by_fingerprint(sarif, "HOTSPOT-LOW-001")
    assert low["level"] == "note"
    assert rules["python:S2245"]["properties"]["security-severity"] == "3.0"


def test_rule_name_from_rules_block() -> None:
    sarif = _build_from(_HOTSPOTS_FIXTURE)
    rule = _rules_by_id(sarif)["python:S2068"]
    assert rule["name"] == "Credentials should not be hard-coded"
    assert rule["shortDescription"]["text"] == "Credentials should not be hard-coded"


def test_rule_name_falls_back_to_key() -> None:
    # The canonical issues fixture carries no rules block, so names fall
    # back to the rule key (still a valid, non-empty shortDescription).
    sarif = _build_from(_ISSUES_FIXTURE)
    rule = _rules_by_id(sarif)["python:S1481"]
    assert rule["name"] == "python:S1481"
    assert rule["shortDescription"]["text"] == "python:S1481"


def test_help_uri_shape() -> None:
    sarif = _build_from(_ISSUES_FIXTURE)
    rule = _rules_by_id(sarif)["python:S2068"]
    assert rule["helpUri"] == f"{_HOST}/coding_rules?open=python:S2068&rule_key=python:S2068"


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_build_is_deterministic() -> None:
    first = json.dumps(_build_from(_ISSUES_FIXTURE), indent=2, sort_keys=True)
    second = json.dumps(_build_from(_ISSUES_FIXTURE), indent=2, sort_keys=True)
    assert first == second


def test_emit_sarif_cli_is_byte_identical(tmp_path: Path) -> None:
    out1 = tmp_path / "a.sarif"
    out2 = tmp_path / "b.sarif"
    rc1 = main(argv=["--emit-sarif", str(out1), "--fixture", str(_ISSUES_FIXTURE)])
    rc2 = main(argv=["--emit-sarif", str(out2), "--fixture", str(_ISSUES_FIXTURE)])
    assert rc1 == 0 and rc2 == 0
    assert out1.read_bytes() == out2.read_bytes()


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def test_emit_sarif_cli_writes_valid_file(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    out = tmp_path / "nested" / "sonar.sarif"
    rc = main(argv=["--emit-sarif", str(out), "--fixture", str(_ISSUES_FIXTURE)])
    assert rc == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["version"] == "2.1.0"
    captured = capsys.readouterr()
    # The canonical fixture has 6 findings, but 4 are pure-maintainability
    # CODE_SMELL findings that the default security scope drops, leaving
    # the VULNERABILITY and BUG (2 rules, 2 results).
    assert "rules=2" in captured.out
    assert "results=2" in captured.out


def test_emit_sarif_writes_no_backlog_tickets(tmp_path: Path) -> None:
    out = tmp_path / "sonar.sarif"
    rc = main(argv=["--emit-sarif", str(out), "--fixture", str(_ISSUES_FIXTURE)])
    assert rc == 0
    # SARIF mode must not emit any Markdown backlog ticket anywhere.
    assert list(tmp_path.rglob("*.md")) == []


# ---------------------------------------------------------------------------
# Security-scope filter (keep the Security tab focused on security)
# ---------------------------------------------------------------------------


def _finding(key: str, ftype: str, *, rule: str | None = None) -> Finding:
    """Build a minimal Finding of a given Sonar type for filter tests."""
    return Finding(
        key=key,
        rule=rule or f"python:{key}",
        severity="MAJOR",
        type=ftype,
        component="bernstein:src/bernstein/core/x.py",
        line=10,
        creation_date="2026-06-01T00:00:00+0000",
    )


def test_code_scanning_types_constant() -> None:
    # The exported set is exactly the security- and reliability-relevant
    # Sonar types. CODE_SMELL (pure maintainability) is deliberately out.
    assert frozenset({"VULNERABILITY", "SECURITY_HOTSPOT", "BUG"}) == SARIF_CODE_SCANNING_TYPES
    assert "CODE_SMELL" not in SARIF_CODE_SCANNING_TYPES


def test_filter_excludes_code_smells_by_default() -> None:
    findings = [
        _finding("V1", "VULNERABILITY"),
        _finding("H1", "SECURITY_HOTSPOT"),
        _finding("B1", "BUG"),
        _finding("C1", "CODE_SMELL"),
        _finding("C2", "CODE_SMELL"),
    ]
    kept = filter_sarif_findings(findings)
    kept_types = {f.type for f in kept}
    assert "CODE_SMELL" not in kept_types
    assert kept_types == {"VULNERABILITY", "SECURITY_HOTSPOT", "BUG"}
    assert {f.key for f in kept} == {"V1", "H1", "B1"}


def test_filter_include_code_smells_keeps_everything() -> None:
    findings = [
        _finding("V1", "VULNERABILITY"),
        _finding("C1", "CODE_SMELL"),
        _finding("C2", "CODE_SMELL"),
    ]
    kept = filter_sarif_findings(findings, include_code_smells=True)
    # Order preserved, nothing dropped when the operator opts into everything.
    assert [f.key for f in kept] == ["V1", "C1", "C2"]


def test_filter_drops_unknown_types_by_default() -> None:
    # The whitelist is conservative: any non-security type (including a
    # hypothetical future Sonar type) is dropped unless smells are opted in.
    findings = [_finding("V1", "VULNERABILITY"), _finding("X1", "SOME_NEW_TYPE")]
    assert [f.key for f in filter_sarif_findings(findings)] == ["V1"]


def test_emit_sarif_drops_code_smell_rules_by_default(tmp_path: Path) -> None:
    out = tmp_path / "sonar.sarif"
    rc = main(argv=["--emit-sarif", str(out), "--fixture", str(_ISSUES_FIXTURE)])
    assert rc == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    rule_ids = {r["id"] for r in doc["runs"][0]["tool"]["driver"]["rules"]}
    # The fixture's four CODE_SMELL rules must not reach code scanning.
    for smell_rule in ("python:S3776", "python:S1192", "python:S1481", "python:S125"):
        assert smell_rule not in rule_ids
    # The VULNERABILITY and BUG rules must remain.
    assert "python:S2068" in rule_ids  # VULNERABILITY
    assert "python:S2589" in rule_ids  # BUG
    fingerprints = {res["partialFingerprints"]["sonarFindingKey"] for res in doc["runs"][0]["results"]}
    assert fingerprints == {"FINDING-BLOCKER-001", "FINDING-BUG-001"}


def test_emit_sarif_keeps_hotspots_and_vulnerabilities(tmp_path: Path) -> None:
    out = tmp_path / "sonar.sarif"
    rc = main(argv=["--emit-sarif", str(out), "--fixture", str(_HOTSPOTS_FIXTURE)])
    assert rc == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    fingerprints = {res["partialFingerprints"]["sonarFindingKey"] for res in doc["runs"][0]["results"]}
    # One VULNERABILITY + two SECURITY_HOTSPOT findings, all retained.
    assert fingerprints == {"ISSUE-VULN-001", "HOTSPOT-HIGH-001", "HOTSPOT-LOW-001"}


def test_emit_sarif_include_code_smells_flag_keeps_all(tmp_path: Path) -> None:
    out = tmp_path / "sonar.sarif"
    rc = main(
        argv=[
            "--emit-sarif",
            str(out),
            "--fixture",
            str(_ISSUES_FIXTURE),
            "--sarif-include-code-smells",
        ]
    )
    assert rc == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert len(doc["runs"][0]["results"]) == 6
    rule_ids = {r["id"] for r in doc["runs"][0]["tool"]["driver"]["rules"]}
    assert "python:S3776" in rule_ids  # a CODE_SMELL rule is retained on opt-in


def test_emit_sarif_include_code_smells_is_deterministic(tmp_path: Path) -> None:
    out1 = tmp_path / "a.sarif"
    out2 = tmp_path / "b.sarif"
    common = ["--fixture", str(_ISSUES_FIXTURE), "--sarif-include-code-smells"]
    assert main(argv=["--emit-sarif", str(out1), *common]) == 0
    assert main(argv=["--emit-sarif", str(out2), *common]) == 0
    assert out1.read_bytes() == out2.read_bytes()
