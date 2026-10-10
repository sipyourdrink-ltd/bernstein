"""Fail-closed, content-addressed gate-evasion regression baseline (#6154).

This is intentionally separate from the general benchmark CI scorecard:
an unverifiable baseline or a broken gate runner must never be neutral here.
The digest authenticates content against the committed, reviewed file, not an
author's identity; it is not a signature or a replacement for code review.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bernstein.eval.bench.gate_evasion_suite import (
    _GATE_TOOL_MODULE,
    DEFAULT_EVASION_CORPUS_DIR,
    build_gate_evasion_suite_v1,
    load_evasion_corpus,
    run_gate_evasion_suite,
)

if TYPE_CHECKING:
    from bernstein.eval.bench.gate_evasion_suite import GateEvasionCase

SCHEMA_VERSION = 1
_POSITIVE_BASIS = frozenset({"finding_signature", "junit_failures"})
_VALID_MISSES = frozenset(
    {
        ("pass", "gate_verdict"),
        ("pass", "junit_failures"),
        ("no_gate", "no_gate"),
    }
)
_CASE_KEYS = frozenset(
    {"case_class", "gate_that_must_flag", "expected_verdict", "actual_verdict", "caught", "verdict_basis"}
)
_BASELINE_KEYS = frozenset(
    {
        "schema_version",
        "suite_version",
        "suite_hash",
        "corpus_sha256",
        "tools",
        "cases",
        "total",
        "caught_count",
        "catch_rate",
        "update_reason",
        "content_sha256",
    }
)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def corpus_bytes_digest(corpus_dir: Path | str = DEFAULT_EVASION_CORPUS_DIR) -> str:
    """Hash sorted corpus-relative paths and original bytes, including every fixture and manifest."""
    root = Path(corpus_dir)
    if not root.is_dir():
        raise ValueError(f"gate-evasion corpus directory missing: {root}")
    entries = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"gate-evasion corpus contains a symlink: {path}")
        if path.is_file():
            entries.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
    if not entries:
        raise ValueError("gate-evasion corpus has no fixture files")
    return _digest(entries)


def required_tool_versions(cases: list[GateEvasionCase]) -> dict[str, str]:
    """Require every command-gate tool in this interpreter, not another uv-tool environment."""
    tools = sorted(
        {_GATE_TOOL_MODULE[c.gate_that_must_flag] for c in cases if c.gate_that_must_flag in _GATE_TOOL_MODULE}
    )
    versions = {}
    for tool in tools:
        if importlib.util.find_spec(tool) is None:
            raise ValueError(f"required gate-evasion tool {tool!r} is missing from this Python interpreter")
        try:
            versions[tool] = importlib.metadata.version(tool)
        except importlib.metadata.PackageNotFoundError as exc:
            raise ValueError(f"required gate-evasion tool {tool!r} has no installed version") from exc
    return versions


def _check_case_result(entry: dict[str, Any]) -> None:
    if set(entry) != _CASE_KEYS or not all(
        isinstance(entry[key], str) and entry[key] for key in _CASE_KEYS - {"caught"}
    ):
        raise ValueError(f"malformed gate-evasion case result: {entry!r}")
    if type(entry["caught"]) is not bool:
        raise ValueError(f"non-boolean caught verdict: {entry['case_class']}")
    verdict = entry["actual_verdict"]
    basis = entry["verdict_basis"]
    if entry["caught"]:
        if (verdict, basis) not in {("fail", "finding_signature"), ("fail", "junit_failures")}:
            raise ValueError(f"{entry['case_class']}: caught without positive finding evidence ({verdict}/{basis})")
    elif (verdict, basis) not in _VALID_MISSES:
        raise ValueError(f"{entry['case_class']}: incomplete gate evaluation or tool/runner error ({verdict}/{basis})")


def _validate_counts(payload: dict[str, Any]) -> None:
    cases = payload["cases"]
    if not isinstance(cases, list) or not cases:
        raise ValueError("gate-evasion results contain no cases")
    ids = [entry.get("case_class") if isinstance(entry, dict) else None for entry in cases]
    if any(not isinstance(i, str) for i in ids) or ids != sorted(set(ids)):
        raise ValueError("gate-evasion cases must have unique, sorted class IDs")
    for entry in cases:
        _check_case_result(entry)
    count = sum(entry["caught"] for entry in cases)
    if type(payload["total"]) is not int or payload["total"] != len(cases):
        raise ValueError("gate-evasion total does not match the case list")
    if type(payload["caught_count"]) is not int or payload["caught_count"] != count:
        raise ValueError("gate-evasion caught_count does not match case findings")
    if type(payload["catch_rate"]) is not float or payload["catch_rate"] != count / len(cases):
        raise ValueError("gate-evasion catch_rate does not match the case findings")


def _corpus_cases(corpus_dir: Path) -> list[GateEvasionCase]:
    cases = load_evasion_corpus(corpus_dir)
    if not cases:
        raise ValueError("gate-evasion corpus has no cases")
    ids = [case.class_name for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("gate-evasion corpus has duplicate class IDs")
    # A directory without a manifest must not quietly fall out of the denominator.
    for path in corpus_dir.iterdir():
        if path.is_dir() and not (path / "manifest.json").is_file():
            raise ValueError(f"gate-evasion corpus case has no manifest: {path}")
    return cases


def measure_gate_evasion(corpus_dir: Path | str = DEFAULT_EVASION_CORPUS_DIR) -> dict[str, Any]:
    """Run all real gates and return a fully checked, unsigned measurement."""
    corpus_dir = Path(corpus_dir)
    cases = _corpus_cases(corpus_dir)
    tools = required_tool_versions(cases)
    suite = build_gate_evasion_suite_v1(corpus_dir)
    if len(suite.tasks) != len(cases) or len({task.id for task in suite.tasks}) != len(cases):
        raise ValueError("suite task list is incomplete or duplicated")
    score, _bundle = run_gate_evasion_suite(corpus_dir=corpus_dir)
    expected = {case.class_name: case for case in cases}
    measured = []
    for result in score.results:
        case = expected.get(result.case_class)
        if case is None:
            raise ValueError(f"unexpected gate-evasion result: {result.case_class}")
        if result.gate_that_must_flag != case.gate_that_must_flag or result.expected_verdict != case.expected_verdict:
            raise ValueError(f"gate-evasion result does not match manifest: {result.case_class}")
        if result.caught and result.flagged_by_gate != result.gate_that_must_flag:
            raise ValueError(f"caught case lacks gate attribution: {result.case_class}")
        measured.append(
            {
                "case_class": result.case_class,
                "gate_that_must_flag": result.gate_that_must_flag,
                "expected_verdict": result.expected_verdict,
                "actual_verdict": result.actual_verdict,
                "caught": result.caught,
                "verdict_basis": result.verdict_basis,
            }
        )
    measured.sort(key=lambda entry: entry["case_class"])
    if len(measured) != len(cases) or {e["case_class"] for e in measured} != set(expected):
        raise ValueError("gate-evasion run did not evaluate every case exactly once")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "suite_version": suite.version,
        "suite_hash": suite.suite_hash,
        "corpus_sha256": corpus_bytes_digest(corpus_dir),
        "tools": tools,
        "cases": measured,
        "total": score.total_cases,
        "caught_count": score.caught_cases,
        "catch_rate": score.catch_rate,
    }
    _validate_counts(payload)
    return payload


def read_baseline(path: Path) -> dict[str, Any]:
    """Validate a committed baseline's schema, arithmetic and canonical content digest."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"gate-evasion baseline {path} is missing or unreadable: {exc}") from exc
    if not isinstance(payload, dict) or set(payload) != _BASELINE_KEYS:
        raise ValueError("gate-evasion baseline has missing or unknown fields")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported gate-evasion baseline schema version")
    for key in ("suite_version", "suite_hash", "corpus_sha256", "update_reason", "content_sha256"):
        if not isinstance(payload[key], str) or not payload[key]:
            raise ValueError(f"invalid gate-evasion baseline {key}")
    if any(
        len(payload[key]) != 64 or any(ch not in "0123456789abcdef" for ch in payload[key])
        for key in ("suite_hash", "corpus_sha256", "content_sha256")
    ):
        raise ValueError("gate-evasion baseline contains an invalid SHA-256 digest")
    if (
        not isinstance(payload["tools"], dict)
        or not payload["tools"]
        or any(not isinstance(k, str) or not isinstance(v, str) or not k or not v for k, v in payload["tools"].items())
    ):
        raise ValueError("gate-evasion baseline has invalid pinned tool versions")
    _validate_counts(payload)
    expected_digest = _digest({k: v for k, v in payload.items() if k != "content_sha256"})
    if payload["content_sha256"] != expected_digest:
        raise ValueError("gate-evasion baseline content SHA-256 mismatch")
    return payload


def compare_to_baseline(baseline: dict[str, Any], measured: dict[str, Any]) -> None:
    """Fail on identity drift, rate regression or any caught-to-missed case."""
    for key in ("schema_version", "suite_version", "suite_hash", "corpus_sha256", "tools"):
        if baseline[key] != measured[key]:
            raise ValueError(f"gate-evasion baseline {key} mismatch; explicitly remeasure with --update --reason")
    reference = {case["case_class"]: case for case in baseline["cases"]}
    current = {case["case_class"]: case for case in measured["cases"]}
    if reference.keys() != current.keys() or baseline["total"] != measured["total"]:
        raise ValueError("gate-evasion baseline case set/denominator mismatch; explicit update required")
    regressed = []
    for name, original in reference.items():
        now = current[name]
        if (original["gate_that_must_flag"], original["expected_verdict"]) != (
            now["gate_that_must_flag"],
            now["expected_verdict"],
        ):
            raise ValueError(f"gate-evasion case {name}: expected gate/verdict changed")
        if original["caught"] and not now["caught"]:
            regressed.append(f"{name} ({original['gate_that_must_flag']})")
    if measured["caught_count"] < baseline["caught_count"]:
        raise ValueError(
            f"gate-evasion catch rate regressed: {measured['caught_count']}/{measured['total']} "
            f"below baseline {baseline['caught_count']}/{baseline['total']}; "
            f"caught-to-missed: {', '.join(regressed)}"
        )
    if regressed:
        raise ValueError(f"gate-evasion caught-to-missed regression: {', '.join(regressed)}")


def check_or_update_baseline(path: Path, *, update: bool = False, reason: str = "") -> dict[str, Any]:
    """Measure the complete pinned suite, then check or explicitly update its baseline."""
    if update:
        if not reason.strip():
            raise ValueError("--update requires a non-empty --reason for the review record")
    elif reason:
        raise ValueError("--reason can only be used with --update")
    reference = None if update else read_baseline(path)
    measurement = measure_gate_evasion()
    if update:
        payload = {**measurement, "update_reason": reason.strip()}
        payload["content_sha256"] = _digest(payload)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
        # Independent self-consistency validation of the persisted artifact.
        return read_baseline(path)
    assert reference is not None
    compare_to_baseline(reference, measurement)
    return measurement
