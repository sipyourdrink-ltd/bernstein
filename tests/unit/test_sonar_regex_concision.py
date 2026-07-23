"""Regression tests for tracked regex-concision findings."""

from __future__ import annotations

from pathlib import Path

from bernstein.core.communication.direct import _MENTION_RE
from bernstein.core.devops.trend_scan import _tokens
from bernstein.core.lifecycle.hook_filter import HookFilterError, parse_hook_filter
from bernstein.core.persistence.action_cache import redact_secrets
from bernstein.core.quality.pr_review_aggregator import _normalise_tokens as aggregate_tokens
from bernstein.core.quality.review_consensus import _normalise_tokens as consensus_tokens
from bernstein.core.tasks.backlog_parser import _STORY_ID, _TASK_ID

REPO_ROOT = Path(__file__).resolve().parents[2]
TRACKED_FILES = (
    Path("src/bernstein/core/communication/direct.py"),
    Path("src/bernstein/core/devops/trend_scan.py"),
    Path("src/bernstein/core/lifecycle/hook_filter.py"),
    Path("src/bernstein/core/persistence/action_cache.py"),
    Path("src/bernstein/core/quality/pr_review_aggregator.py"),
    Path("src/bernstein/core/quality/review_consensus.py"),
    Path("src/bernstein/core/tasks/backlog_parser.py"),
)
OLD_REGEX_FRAGMENTS = (
    "[A-Za-z0-9]",
    "[A-Za-z_]",
    "[A-Za-z][\\w+-]{1,}",
    "[A-Za-z0-9_\\-]",
    "[A-Za-z0-9]{20,}",
    "[0-9",
    "{1,}",
    "[^\\w]+",
)


def test_tracked_regexes_use_concise_forms() -> None:
    findings: list[tuple[Path, str]] = []
    for path in TRACKED_FILES:
        text = (REPO_ROOT / path).read_text(encoding="utf-8")
        for fragment in OLD_REGEX_FRAGMENTS:
            if fragment in text:
                findings.append((path, fragment))

    assert findings == []


def test_regex_behavior_is_preserved_for_representative_inputs() -> None:
    assert _MENTION_RE.findall("wake @abc-123 and user@example.com") == ["abc-123"]
    assert _MENTION_RE.findall("skip @_hidden and @9agent") == ["9agent"]

    assert _tokens("fix bug, x, ai, release+note alpha_1 123bad") == [
        "fix",
        "bug",
        "ai",
        "release+note",
        "alpha_1",
        "bad",
    ]

    assert parse_hook_filter("Bash(git *)") is not None
    try:
        parse_hook_filter("1Tool(name)")
    except HookFilterError:
        pass
    else:  # pragma: no cover - assertion branch
        raise AssertionError("digit-leading tool names must stay invalid")

    redacted = redact_secrets("sk-" + "A" * 20 + " ghp_" + "C" * 20 + " AKIA" + "D" * 16)
    assert "[REDACTED_OPENAI_KEY]" in redacted
    assert "[REDACTED_GH_TOKEN]" in redacted
    assert "[REDACTED_AWS_KEY]" in redacted

    assert aggregate_tokens("A retry-safe review_1 123skip") == frozenset({"retry", "safe", "review_1", "skip"})
    assert consensus_tokens("A retry-safe review_1 123skip") == frozenset({"retry", "safe", "review_1", "skip"})

    assert _TASK_ID.match("T123-alpha") is not None
    assert _TASK_ID.match("Tabc") is None
    assert _STORY_ID.match("US123-alpha") is not None
    assert _STORY_ID.match("USabc") is None
