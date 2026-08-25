"""Review-ruleset tests (issue #4481).

The ruleset is the standard a verdict was produced under.  These tests pin
the two properties that make it usable as provenance:

* AC4 -- raise and guard rules are loaded, the digest is stable under
  reordering but not under an edit, and guard rules reach the reviewer.
* AC5 -- with no rules file the pipeline is unchanged and the digest is the
  digest of the empty set.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.quality.review_pipeline.ruleset import (
    EMPTY_RULESET,
    ReviewRulesetError,
    RulesSpec,
    load_ruleset,
    parse_ruleset,
)

_RULES = """# Review rules

## Raise

- Bare `except:` that swallows the traceback.
- A subprocess call without a timeout.

## Guard

- Do not re-report `assert` in tests as a security finding.
"""

_REORDERED = """# Review rules

## Guard

- Do not re-report `assert` in tests as a security finding.

## Raise

- A subprocess call without a timeout.
- Bare `except:` that swallows the traceback.
"""


def _write(root: Path, text: str, relpath: str = ".bernstein/review-rules.md") -> Path:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# AC4 -- loading, digest stability, guard visibility
# ---------------------------------------------------------------------------


def test_raise_and_guard_rules_are_parsed_from_the_rules_file(tmp_path: Path) -> None:
    _write(tmp_path, _RULES)
    ruleset = load_ruleset(repo_root=tmp_path)

    assert [r.text for r in ruleset.raise_rules] == [
        "Bare `except:` that swallows the traceback.",
        "A subprocess call without a timeout.",
    ]
    assert [r.text for r in ruleset.guard_rules] == [
        "Do not re-report `assert` in tests as a security finding.",
    ]
    assert not ruleset.is_empty


def test_ruleset_digest_is_stable_under_rule_reordering() -> None:
    assert parse_ruleset(_RULES).digest == parse_ruleset(_REORDERED).digest


def test_ruleset_digest_changes_when_a_rule_body_changes() -> None:
    edited = _RULES.replace("without a timeout", "without an explicit timeout")

    assert parse_ruleset(edited).digest != parse_ruleset(_RULES).digest


def test_pipeline_rules_key_points_at_another_rules_file(tmp_path: Path) -> None:
    _write(tmp_path, _RULES)
    _write(tmp_path, "## Raise\n\n- Only this one.\n", relpath="review/house-rules.md")

    ruleset = load_ruleset(repo_root=tmp_path, rules="review/house-rules.md")

    assert [r.text for r in ruleset.raise_rules] == ["Only this one."]
    assert ruleset.guard_rules == ()
    assert ruleset.digest != parse_ruleset(_RULES).digest


def test_inline_pipeline_rules_extend_the_rules_file(tmp_path: Path) -> None:
    _write(tmp_path, _RULES)
    spec = RulesSpec.model_validate({"guard": ["Do not flag the vendored parser."]})

    ruleset = load_ruleset(repo_root=tmp_path, rules=spec)

    assert [r.text for r in ruleset.guard_rules] == [
        "Do not re-report `assert` in tests as a security finding.",
        "Do not flag the vendored parser.",
    ]
    assert ruleset.digest != parse_ruleset(_RULES).digest


def test_explicitly_named_missing_rules_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ReviewRulesetError, match="review/house-rules.md"):
        load_ruleset(repo_root=tmp_path, rules="review/house-rules.md")


def test_guard_rules_are_labelled_as_findings_not_to_raise(tmp_path: Path) -> None:
    _write(tmp_path, _RULES)
    section = load_ruleset(repo_root=tmp_path).to_prompt_section()

    assert "Do not re-report `assert` in tests as a security finding." in section
    assert "must not" in section.lower()


# ---------------------------------------------------------------------------
# AC5 -- no rules file
# ---------------------------------------------------------------------------


def test_missing_rules_file_yields_the_empty_set_digest(tmp_path: Path) -> None:
    ruleset = load_ruleset(repo_root=tmp_path)

    assert ruleset.is_empty
    assert ruleset.digest == EMPTY_RULESET.digest
    assert ruleset.digest.startswith("sha256:")


def test_empty_ruleset_contributes_no_prompt_section() -> None:
    assert EMPTY_RULESET.to_prompt_section() == ""
