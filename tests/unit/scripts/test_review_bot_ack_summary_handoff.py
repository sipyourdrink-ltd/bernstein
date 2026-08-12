"""The gate's summary has to survive a token that cannot post it.

``scripts/review_bot_ack.py`` already renders the sentence that matters - "1 of
1 configured review bots produced no review for this head commit, so the
finding counts above are not a clean result". On a pull request from a fork the
``GITHUB_TOKEN`` is read-only and a ``permissions:`` block cannot raise it, so
posting that text as the sticky comment returns ``403 Resource not accessible
by integration``. The warning was computed correctly and written only to a
workflow log, while the published check summary told the reader to consult a
sticky comment that did not exist.

These tests pin the handoff that fixes it: the runner writes the rendered
summary to a file whatever the post does, and the publisher - which runs in the
base repository with a writable token - posts that file verbatim without
re-evaluating. Re-evaluating there would be wrong: the verdict belongs to the
run that produced the text, and a later evaluation can reach a different one.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Generator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "review_bot_ack.py"


@pytest.fixture
def ack() -> Generator[ModuleType, None, None]:
    """Load scripts/review_bot_ack.py as an importable module."""
    spec = importlib.util.spec_from_file_location("review_bot_ack_handoff_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


def _clean_outcome(ack: ModuleType) -> Any:
    """An outcome with no findings and one bot that never reviewed the head."""
    status = ack.BotStatus(
        login="baz-reviewer[bot]",
        status=ack.BOT_ABSENT,
        detail="left no review or comment",
    )
    return ack.GateOutcome(head_sha="0" * 40, bot_statuses=[status])


def test_summary_is_written_even_when_the_sticky_post_fails(
    ack: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fork case is exactly the case where the post fails."""
    monkeypatch.setenv("GH_TOKEN", "t")
    monkeypatch.setattr(ack, "evaluate", lambda *a, **k: _clean_outcome(ack))

    def forbidden(*_a: object, **_k: object) -> None:
        raise RuntimeError("403 Resource not accessible by integration")

    monkeypatch.setattr(ack, "upsert_sticky", forbidden)

    out = tmp_path / "verdict" / "summary.md"
    rc = ack.main(
        ["--owner", "o", "--repo", "r", "--pr", "1", "--summary-out", str(out)]
    )

    assert rc == 0, "an unreviewed bot must not fail the gate by default"
    assert out.exists(), "the summary was lost with the failed post"
    written = out.read_text(encoding="utf-8")
    assert ack.STICKY_HEADER in written
    assert "produced no review for this head commit" in written, (
        "the handed-over text omits the only sentence that says the gate is not evidence"
    )


def test_post_summary_file_posts_verbatim_without_evaluating(
    ack: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Publisher mode speaks for the run that rendered the text, not for now."""
    monkeypatch.setenv("GH_TOKEN", "t")

    def must_not_run(*_a: object, **_k: object) -> None:
        raise AssertionError("publisher mode re-evaluated the pull request")

    monkeypatch.setattr(ack, "evaluate", must_not_run)

    posted: dict[str, Any] = {}
    monkeypatch.setattr(
        ack,
        "upsert_sticky",
        lambda owner, repo, pr, token, body: posted.update(pr=pr, body=body),
    )

    handed = tmp_path / "summary.md"
    handed.write_text("### handed over\n", encoding="utf-8")

    rc = ack.main(
        ["--owner", "o", "--repo", "r", "--pr", "42", "--post-summary-file", str(handed)]
    )

    assert rc == 0
    assert posted == {"pr": 42, "body": "### handed over\n"}


def test_post_summary_file_fails_loudly_when_the_post_fails(
    ack: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A silent failure here is the bug this whole handoff exists to remove."""
    monkeypatch.setenv("GH_TOKEN", "t")

    def forbidden(*_a: object, **_k: object) -> None:
        raise RuntimeError("403")

    monkeypatch.setattr(ack, "upsert_sticky", forbidden)

    handed = tmp_path / "summary.md"
    handed.write_text("### handed over\n", encoding="utf-8")

    rc = ack.main(
        ["--owner", "o", "--repo", "r", "--pr", "42", "--post-summary-file", str(handed)]
    )

    assert rc == 2


def test_post_summary_file_missing_is_an_error_not_a_pass(
    ack: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An absent hand-over file means the runner changed; do not report success."""
    monkeypatch.setenv("GH_TOKEN", "t")
    monkeypatch.setattr(ack, "upsert_sticky", lambda *a, **k: None)

    rc = ack.main(
        [
            "--owner",
            "o",
            "--repo",
            "r",
            "--pr",
            "42",
            "--post-summary-file",
            str(tmp_path / "absent.md"),
        ]
    )

    assert rc == 2
