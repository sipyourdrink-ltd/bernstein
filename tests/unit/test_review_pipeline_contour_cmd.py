"""CLI wiring for the fix-until-green contour (issue #4481).

The loop lives in :mod:`bernstein.core.quality.review_pipeline.contour`; these
tests pin that the CLI only assembles its inputs and forwards the outcome's
exit code (AC1).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bernstein.cli.commands import review_pipeline_cmd
from bernstein.core.quality.review_pipeline.contour import ContourResult, PassRecord

_PIPELINE_YAML = """version: 1
name: contour
stages:
  - name: s
    agents:
      - role: r
"""

_RULES = "## Guard\n\n- Do not flag the vendored parser.\n"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "review.yaml").write_text(_PIPELINE_YAML, encoding="utf-8")
    rules = tmp_path / ".bernstein" / "review-rules.md"
    rules.parent.mkdir(parents=True, exist_ok=True)
    rules.write_text(_RULES, encoding="utf-8")
    return tmp_path


def _stub_contour(monkeypatch: pytest.MonkeyPatch, result: ContourResult) -> dict[str, Any]:
    """Stub every collaborator that would touch GitHub or the install identity."""
    seen: dict[str, Any] = {}

    def _fake(pipeline: Any, **kwargs: Any) -> ContourResult:
        seen["pipeline"] = pipeline
        seen.update(kwargs)
        return result

    monkeypatch.setattr(review_pipeline_cmd, "run_review_contour", _fake)
    monkeypatch.setattr(
        review_pipeline_cmd,
        "gh_pr_view_json",
        lambda *_a, **_k: {"url": "https://github.com/acme/widget/pull/7", "body": "ticket"},
    )
    monkeypatch.setattr(review_pipeline_cmd, "receipt_emitter", lambda **_k: "emitter")
    return seen


def test_cli_returns_nonzero_when_the_contour_needs_an_operator(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    record = PassRecord(
        index=1,
        verdict="request_changes",
        checks_state="red",
        diff_hash="sha256:x",
        ruleset_digest="sha256:y",
    )
    seen = _stub_contour(
        monkeypatch,
        ContourResult(outcome="needs-operator", reason="max_passes=1 spent", passes=(record,)),
    )

    code = review_pipeline_cmd.run_review_pipeline_cli(
        pipeline_path=str(project / "review.yaml"),
        pr_number=7,
        validate_only=False,
        dry_run=False,
        workdir=str(project),
        until_checks_green=True,
        max_passes=1,
    )

    assert code == 1
    assert seen["max_passes"] == 1


def test_cli_hands_the_loaded_ruleset_to_the_contour(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _stub_contour(monkeypatch, ContourResult(outcome="approved", reason="", passes=()))

    code = review_pipeline_cmd.run_review_pipeline_cli(
        pipeline_path=str(project / "review.yaml"),
        pr_number=7,
        validate_only=False,
        dry_run=False,
        workdir=str(project),
        until_checks_green=True,
        max_passes=3,
    )

    assert code == 0
    assert [r.text for r in seen["ruleset"].guard_rules] == ["Do not flag the vendored parser."]


def test_cli_without_a_fix_command_supplies_no_fix_runner(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _stub_contour(monkeypatch, ContourResult(outcome="approved", reason="", passes=()))

    review_pipeline_cmd.run_review_pipeline_cli(
        pipeline_path=str(project / "review.yaml"),
        pr_number=7,
        validate_only=False,
        dry_run=False,
        workdir=str(project),
        fix=True,
        until_checks_green=True,
    )

    assert seen["fix_runner"] is None


def test_repo_slug_is_derived_from_the_pr_url_offline() -> None:
    assert review_pipeline_cmd._repo_slug("https://github.com/acme/widget/pull/42") == "acme/widget"
    assert review_pipeline_cmd._repo_slug("https://ghe.example.com/team/tool/pull/7") == "team/tool"
    assert review_pipeline_cmd._repo_slug("not-a-pr-url") == ""
