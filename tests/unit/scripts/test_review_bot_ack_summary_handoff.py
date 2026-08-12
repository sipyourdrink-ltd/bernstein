"""The gate's summary has to survive a token that cannot post it.

``scripts/review_bot_ack.py`` already renders the sentence that matters - "1 of
1 configured review bots produced no review for this head commit, so the
finding counts above are not a clean result". On a pull request from a fork the
``GITHUB_TOKEN`` is read-only and a ``permissions:`` block cannot raise it, so
posting that text as the sticky comment returns ``403 Resource not accessible
by integration``. The warning was computed correctly and written only to a
workflow log, while the published check summary told the reader to consult a
sticky comment that did not exist.

These tests pin the handoff that fixes it. The runner captures stdout into the
artifact, so the first test pins the stdout contract the workflow relies on:
the summary reaches stdout whatever the post does, and nothing else does. That
contract is what makes the capture work from the base checkout - the gate job
runs the base branch's copy of this script, so it cannot use a flag introduced
by the pull request that needs it.

The publisher - which runs in the base repository with a writable token - then
posts that captured text verbatim without re-evaluating. Re-evaluating there
would be wrong: the verdict belongs to the run that produced the text, and a
later evaluation can reach a different one.
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


def test_stdout_carries_the_summary_when_the_sticky_post_fails(
    ack: ModuleType,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fork case is exactly the case where the post fails.

    The runner tees this stdout into ``verdict/summary.md``. Anything else
    printed to stdout would land in the artifact and be posted as the comment,
    so this asserts stdout equals the summary rather than merely containing it.
    """
    monkeypatch.setenv("GH_TOKEN", "t")
    monkeypatch.setattr(ack, "evaluate", lambda *a, **k: _clean_outcome(ack))

    def forbidden(*_a: object, **_k: object) -> None:
        raise RuntimeError("403 Resource not accessible by integration")

    monkeypatch.setattr(ack, "upsert_sticky", forbidden)

    rc = ack.main(["--owner", "o", "--repo", "r", "--pr", "1"])

    assert rc == 0, "an unreviewed bot must not fail the gate by default"
    captured = capsys.readouterr()
    assert captured.out == ack.render_summary(_clean_outcome(ack)) + "\n", (
        "stdout is the artifact the publisher posts; it must be the summary and nothing else"
    )
    assert ack.STICKY_HEADER in captured.out
    assert "produced no review for this head commit" in captured.out, (
        "the handed-over text omits the only sentence that says the gate is not evidence"
    )
    assert "403" in captured.err, "the failed post must still be reported somewhere"


def _rendered(ack: ModuleType) -> str:
    """What the gate job actually hands over: the text render_summary produces."""
    return ack.render_summary(_clean_outcome(ack)) + "\n"


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
    handed.write_text(_rendered(ack), encoding="utf-8")

    rc = ack.main(["--owner", "o", "--repo", "r", "--pr", "42", "--post-summary-file", str(handed)])

    assert rc == 0
    assert posted == {"pr": 42, "body": _rendered(ack)}


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
    handed.write_text(_rendered(ack), encoding="utf-8")

    rc = ack.main(["--owner", "o", "--repo", "r", "--pr", "42", "--post-summary-file", str(handed)])

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


# The hand-over file crosses a trust boundary. On a fork pull request the gate
# job runs the fork's checkout, so every byte in the artifact is caller input,
# while the publisher that reads it holds a base-repository token that can
# write pull request comments. These pin the two halves of the answer: the
# publisher posts only what looks like this script's own summary, and the
# workflow around it refuses a pull request number that is not the one at the
# head SHA the run was triggered for.


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("empty", ""),
        ("arbitrary markdown", "### handed over\n"),
        ("header without the heading", "<!-- review-bot-ack-summary: managed -->\nhi\n"),
        ("heading without the header", "## Review-bot acknowledgement summary\n"),
        ("header not first", "hi\n<!-- review-bot-ack-summary: managed -->\n"),
    ],
)
def test_post_summary_file_refuses_text_that_is_not_a_rendered_summary(
    ack: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    content: str,
) -> None:
    """A writable token must not become a general-purpose comment writer."""
    monkeypatch.setenv("GH_TOKEN", "t")

    def must_not_post(*_a: object, **_k: object) -> None:
        raise AssertionError(f"posted {name} as the sticky comment")

    monkeypatch.setattr(ack, "upsert_sticky", must_not_post)

    handed = tmp_path / "summary.md"
    handed.write_text(content, encoding="utf-8")

    rc = ack.main(["--owner", "o", "--repo", "r", "--pr", "42", "--post-summary-file", str(handed)])

    assert rc == 2


def test_post_summary_file_reports_invalid_utf8_as_a_read_failure(
    ack: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Undecodable bytes are a different fault from a well-formed wrong shape."""
    monkeypatch.setenv("GH_TOKEN", "t")
    monkeypatch.setattr(ack, "upsert_sticky", lambda *a, **k: None)

    handed = tmp_path / "summary.md"
    handed.write_bytes(b"\xff\xfe not utf-8")

    rc = ack.main(["--owner", "o", "--repo", "r", "--pr", "42", "--post-summary-file", str(handed)])

    assert rc == 2
    assert "UTF-8" in capsys.readouterr().err, "an undecodable hand-over must say so rather than raise"


def test_render_summary_satisfies_the_shape_the_publisher_requires(ack: ModuleType) -> None:
    """The check and the renderer must not be able to drift apart."""
    rendered = ack.render_summary(_clean_outcome(ack))
    assert rendered.startswith(ack.STICKY_HEADER)
    assert ack.SUMMARY_HEADING in rendered


def test_publisher_workflow_binds_the_pull_request_to_the_triggering_head() -> None:
    """A numeric pull request number is still someone else's pull request.

    ``workflow_run.head_sha`` is event metadata rather than artifact content,
    so it is the one value in this step the caller cannot choose. The step has
    to compare the named pull request's head against it and refuse a mismatch.
    """
    workflow = (REPO_ROOT / ".github" / "workflows" / "review-bot-ack-publish.yml").read_text(encoding="utf-8")
    step = workflow.split("Post the sticky summary the gate could not", 1)[1]
    step = step.split("\n  republish:", 1)[0]

    assert "HEAD_SHA: ${{ github.event.workflow_run.head_sha }}" in step
    assert ".head.sha" in step, "the step must read the claimed pull request's head"
    assert '!= "$HEAD_SHA"' in step, "the step must refuse a head that is not the trigger's"
    refusal = step.index('!= "$HEAD_SHA"')
    assert step.index("--post-summary-file") > refusal, "the binding check has to run before the post, not after it"


def test_check_summary_only_points_at_a_comment_that_was_handed_over() -> None:
    """A green check must not send the reader after a comment that is not there.

    The post is best-effort by design - a comment that cannot be written must
    not turn a decided verdict into a failure - so the summary text has to say
    which of the two happened. A fixed "see the sticky summary comment" reads
    identically whether the comment exists or the handoff was empty.
    """
    workflow = (REPO_ROOT / ".github" / "workflows" / "review-bot-ack-publish.yml").read_text(encoding="utf-8")

    assert "POINTER=$pointer" in workflow
    assert "${POINTER}" in workflow, "the published summary must use the resolved pointer"
    assert workflow.count("See the sticky summary comment on the pull request") == 1, (
        "the pointer sentence must exist only inside the branch that checked for the file"
    )
    resolve = workflow.index('pointer=""')
    assert resolve < workflow.index("${POINTER}"), "the pointer is resolved before it is published"
