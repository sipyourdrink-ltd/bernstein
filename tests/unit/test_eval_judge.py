"""Tests for the LLM judge — verdict parsing, circuit breaker, resilience."""

from __future__ import annotations

import json

import pytest

from bernstein.eval.judge import JudgeVerdict, _parse_verdict


# ---------------------------------------------------------------------------
# JudgeVerdict dataclass
# ---------------------------------------------------------------------------


class TestJudgeVerdict:
    def test_defaults(self) -> None:
        v = JudgeVerdict()
        assert v.correctness == 0
        assert v.style == 0
        assert v.test_coverage == 0
        assert v.safety == 0
        assert v.verdict == "FAIL"
        assert v.issues == []

    def test_average_score_zero(self) -> None:
        v = JudgeVerdict()
        assert v.average_score == 0.0

    def test_average_score_perfect(self) -> None:
        v = JudgeVerdict(correctness=5, style=5, test_coverage=5, safety=5, verdict="PASS")
        assert v.average_score == pytest.approx(1.0)

    def test_average_score_mixed(self) -> None:
        v = JudgeVerdict(correctness=4, style=3, test_coverage=4, safety=5)
        # (4+3+4+5) / 20 = 16/20 = 0.8
        assert v.average_score == pytest.approx(0.8)

    def test_issues_list(self) -> None:
        v = JudgeVerdict(issues=["bad naming", "missing test"])
        assert len(v.issues) == 2

    def test_frozen(self) -> None:
        v = JudgeVerdict()
        with pytest.raises(AttributeError):
            v.correctness = 5  # type: ignore[misc]


# ---------------------------------------------------------------------------
# _parse_verdict — clean JSON
# ---------------------------------------------------------------------------


class TestParseVerdictCleanJson:
    def test_valid_json(self) -> None:
        raw = json.dumps(
            {
                "correctness": 4,
                "style": 3,
                "test_coverage": 4,
                "safety": 5,
                "verdict": "PASS",
                "issues": [],
            }
        )
        v = _parse_verdict(raw)
        assert v.correctness == 4
        assert v.style == 3
        assert v.test_coverage == 4
        assert v.safety == 5
        assert v.verdict == "PASS"
        assert v.issues == []

    def test_fail_verdict(self) -> None:
        raw = json.dumps(
            {
                "correctness": 2,
                "style": 1,
                "test_coverage": 0,
                "safety": 4,
                "verdict": "FAIL",
                "issues": ["missing tests", "bad naming"],
            }
        )
        v = _parse_verdict(raw)
        assert v.verdict == "FAIL"
        assert len(v.issues) == 2

    def test_missing_fields_default_to_zero(self) -> None:
        raw = json.dumps({"verdict": "FAIL"})
        v = _parse_verdict(raw)
        assert v.correctness == 0
        assert v.style == 0
        assert v.test_coverage == 0
        assert v.safety == 0


# ---------------------------------------------------------------------------
# _parse_verdict — LLM response quirks
# ---------------------------------------------------------------------------


class TestParseVerdictQuirks:
    def test_markdown_code_fence(self) -> None:
        raw = '```json\n{"correctness": 3, "style": 3, "test_coverage": 3, "safety": 3, "verdict": "PASS", "issues": []}\n```'
        v = _parse_verdict(raw)
        assert v.verdict == "PASS"
        assert v.correctness == 3

    def test_json_with_surrounding_text(self) -> None:
        raw = 'Here is my review:\n{"correctness": 4, "style": 4, "test_coverage": 3, "safety": 5, "verdict": "PASS", "issues": []}\nThat is my verdict.'
        v = _parse_verdict(raw)
        assert v.verdict == "PASS"
        assert v.correctness == 4

    def test_whitespace_padding(self) -> None:
        raw = '   \n  {"correctness": 2, "style": 2, "test_coverage": 2, "safety": 2, "verdict": "FAIL", "issues": ["low quality"]}  \n  '
        v = _parse_verdict(raw)
        assert v.verdict == "FAIL"
        assert v.issues == ["low quality"]


# ---------------------------------------------------------------------------
# _parse_verdict — score clamping
# ---------------------------------------------------------------------------


class TestParseVerdictClamping:
    def test_scores_above_max_clamped(self) -> None:
        raw = json.dumps(
            {
                "correctness": 10,
                "style": 99,
                "test_coverage": 7,
                "safety": 6,
                "verdict": "PASS",
                "issues": [],
            }
        )
        v = _parse_verdict(raw)
        assert v.correctness == 5
        assert v.style == 5
        assert v.test_coverage == 5
        assert v.safety == 5

    def test_negative_scores_clamped(self) -> None:
        raw = json.dumps(
            {
                "correctness": -1,
                "style": -5,
                "test_coverage": -10,
                "safety": -3,
                "verdict": "FAIL",
                "issues": [],
            }
        )
        v = _parse_verdict(raw)
        assert v.correctness == 0
        assert v.style == 0
        assert v.test_coverage == 0
        assert v.safety == 0

    def test_float_scores_truncated(self) -> None:
        raw = json.dumps(
            {
                "correctness": 3.7,
                "style": 4.2,
                "test_coverage": 2.9,
                "safety": 4.5,
                "verdict": "PASS",
                "issues": [],
            }
        )
        v = _parse_verdict(raw)
        assert v.correctness == 3
        assert v.style == 4
        assert v.test_coverage == 2
        assert v.safety == 4


# ---------------------------------------------------------------------------
# _parse_verdict — verdict normalization
# ---------------------------------------------------------------------------


class TestParseVerdictNormalization:
    def test_lowercase_pass(self) -> None:
        raw = json.dumps({"verdict": "pass", "issues": []})
        v = _parse_verdict(raw)
        assert v.verdict == "PASS"

    def test_lowercase_fail(self) -> None:
        raw = json.dumps({"verdict": "fail", "issues": []})
        v = _parse_verdict(raw)
        assert v.verdict == "FAIL"

    def test_unknown_verdict_defaults_fail(self) -> None:
        raw = json.dumps({"verdict": "MAYBE", "issues": []})
        v = _parse_verdict(raw)
        assert v.verdict == "FAIL"

    def test_missing_verdict_defaults_fail(self) -> None:
        raw = json.dumps({"issues": []})
        v = _parse_verdict(raw)
        assert v.verdict == "FAIL"


# ---------------------------------------------------------------------------
# _parse_verdict — error cases
# ---------------------------------------------------------------------------


class TestParseVerdictErrors:
    def test_invalid_json_raises(self) -> None:
        with pytest.raises((json.JSONDecodeError, ValueError)):
            _parse_verdict("not json at all")

    def test_empty_string_raises(self) -> None:
        with pytest.raises((json.JSONDecodeError, ValueError)):
            _parse_verdict("")

    def test_non_dict_json_raises(self) -> None:
        with pytest.raises((AttributeError, TypeError)):
            _parse_verdict("[1, 2, 3]")

    def test_issues_non_list_handled(self) -> None:
        raw = json.dumps({"verdict": "FAIL", "issues": "single issue"})
        v = _parse_verdict(raw)
        # Non-list issues should result in empty list
        assert v.issues == []
