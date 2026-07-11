"""Tests for role-template rewrite validators (issue #2249, AC1).

Adversarial fixtures: every rewrite that alters a fenced block, drops a
URL, or touches the completion-instruction block must be rejected by
the mechanical validators, plus the template-specific invariants
(frontmatter, headings, inline code, placeholders) and the two-retry
targeted fix loop.
"""

from __future__ import annotations

import pytest

from bernstein.core.tokens.compaction_validate import all_passed
from bernstein.core.tokens.template_compress_validate import (
    ALL_TEMPLATE_VALIDATOR_NAMES,
    TEMPLATE_MAX_FIX_RETRIES,
    extract_completion_block,
    extract_headings,
    run_template_validators,
    split_frontmatter,
    validate_completion_block,
    validate_frontmatter,
    validate_headings,
    validate_inline_code,
    validate_placeholders,
    validate_template_rewrite,
    validate_urls,
)

# ---------------------------------------------------------------------------
# Fixture template (representative of templates/roles/*/task_prompt.md)
# ---------------------------------------------------------------------------

TEMPLATE = """\
# Task: {{TASK_TITLE}}

## Description
{{TASK_DESCRIPTION}}

{{#IF FILES}}
## Files to work with
{{FILES}}
{{/IF}}

## Instructions
1. Read all listed files carefully before you start writing any code at all
2. Run `uv run ruff check src/` before marking the task as complete
3. Consult https://docs.example.test/style for the full style guide

## Verify
```bash
uv run python scripts/run_tests.py -x
```

{{INCLUDE completion_contract}}
"""

COMPRESSED_OK = """\
# Task: {{TASK_TITLE}}

## Description
{{TASK_DESCRIPTION}}

{{#IF FILES}}
## Files to work with
{{FILES}}
{{/IF}}

## Instructions
1. Read listed files first
2. Run `uv run ruff check src/` before completing
3. Style guide: https://docs.example.test/style

## Verify
```bash
uv run python scripts/run_tests.py -x
```

{{INCLUDE completion_contract}}
"""

EXPANDED_TEMPLATE = """\
# Task: {{TASK_TITLE}}

## Instructions
Do the work described in {{TASK_DESCRIPTION}} carefully and completely.

## Done signal (completion contract worker-completion/v1)

Report your terminal outcome as a structured JSON payload.

```bash
curl -s -X POST http://127.0.0.1:8052/tasks/{{TASK_ID}}/complete
```

Only retry on connection refused errors.

## Bulletin board
Post discoveries so parallel agents stay informed.
"""


def _failed_names(pre: str, post: str) -> set[str]:
    return {v.name for v in run_template_validators(pre, post) if not v.passed}


# ---------------------------------------------------------------------------
# AC1 adversarial fixtures
# ---------------------------------------------------------------------------


class TestAdversarialRewrites:
    def test_good_compression_passes_every_validator(self) -> None:
        verdicts = run_template_validators(TEMPLATE, COMPRESSED_OK)
        assert all_passed(verdicts), [v for v in verdicts if not v.passed]
        assert tuple(v.name for v in verdicts) == ALL_TEMPLATE_VALIDATOR_NAMES

    def test_altered_fenced_block_is_rejected(self) -> None:
        tampered = COMPRESSED_OK.replace(
            "uv run python scripts/run_tests.py -x",
            "uv run pytest tests/",
        )
        assert "code_blocks" in _failed_names(TEMPLATE, tampered)

    def test_dropped_url_is_rejected(self) -> None:
        tampered = COMPRESSED_OK.replace("3. Style guide: https://docs.example.test/style\n", "")
        assert "urls" in _failed_names(TEMPLATE, tampered)

    def test_invented_url_is_rejected(self) -> None:
        tampered = COMPRESSED_OK + "\nSee also https://other.example.test/notes\n"
        assert "urls" in _failed_names(TEMPLATE, tampered)

    def test_dropped_include_directive_is_rejected(self) -> None:
        tampered = COMPRESSED_OK.replace("{{INCLUDE completion_contract}}\n", "")
        failed = _failed_names(TEMPLATE, tampered)
        assert "completion_block" in failed

    def test_reworded_expanded_completion_block_is_rejected(self) -> None:
        tampered = EXPANDED_TEMPLATE.replace(
            "Report your terminal outcome as a structured JSON payload.",
            "Send a JSON payload when done.",
        )
        verdict = validate_completion_block(EXPANDED_TEMPLATE, tampered)
        assert not verdict.passed
        assert "verbatim" in verdict.detail

    def test_expanded_completion_block_survives_verbatim(self) -> None:
        shortened = EXPANDED_TEMPLATE.replace(
            "Do the work described in {{TASK_DESCRIPTION}} carefully and completely.",
            "Do {{TASK_DESCRIPTION}} carefully and completely.",
        )
        assert validate_completion_block(EXPANDED_TEMPLATE, shortened).passed


# ---------------------------------------------------------------------------
# Template-specific invariants
# ---------------------------------------------------------------------------


class TestFrontmatter:
    FRONT = "---\nrole: backend\n---\n# Body\n\nProse here.\n"

    def test_split_round_trips(self) -> None:
        front, body = split_frontmatter(self.FRONT)
        assert front == "---\nrole: backend\n---\n"
        assert front + body == self.FRONT

    def test_no_frontmatter_returns_text_unchanged(self) -> None:
        front, body = split_frontmatter("# Heading\n")
        assert front == ""
        assert body == "# Heading\n"

    def test_unterminated_frontmatter_is_not_split(self) -> None:
        text = "---\nrole: backend\n# Body\n"
        assert split_frontmatter(text) == ("", text)

    def test_edited_frontmatter_is_rejected(self) -> None:
        tampered = self.FRONT.replace("role: backend", "role: qa")
        assert not validate_frontmatter(self.FRONT, tampered).passed

    def test_verbatim_frontmatter_passes(self) -> None:
        shorter = self.FRONT.replace("Prose here.", "Prose.")
        assert validate_frontmatter(self.FRONT, shorter).passed


class TestHeadings:
    def test_reworded_heading_is_rejected(self) -> None:
        tampered = COMPRESSED_OK.replace("## Instructions", "## Steps")
        verdict = validate_headings(TEMPLATE, tampered)
        assert not verdict.passed
        assert "Instructions" in verdict.detail

    def test_dropped_heading_is_rejected(self) -> None:
        tampered = COMPRESSED_OK.replace("## Verify\n", "")
        assert not validate_headings(TEMPLATE, tampered).passed

    def test_invented_heading_is_rejected(self) -> None:
        tampered = COMPRESSED_OK + "\n## Bonus section\n"
        assert not validate_headings(TEMPLATE, tampered).passed

    def test_hash_lines_inside_fences_are_not_headings(self) -> None:
        text = "# Real\n```bash\n# not a heading\n```\n"
        assert extract_headings(text) == ["# Real"]


class TestInlineCodeAndPlaceholders:
    def test_dropped_inline_code_is_rejected(self) -> None:
        tampered = COMPRESSED_OK.replace("`uv run ruff check src/`", "the linter")
        verdict = validate_inline_code(TEMPLATE, tampered)
        assert not verdict.passed
        assert "uv run ruff check src/" in verdict.detail

    def test_invented_inline_code_is_rejected(self) -> None:
        tampered = COMPRESSED_OK + "\nAlso run `make lint` now.\n"
        assert not validate_inline_code(TEMPLATE, tampered).passed

    def test_dropped_placeholder_is_rejected(self) -> None:
        tampered = COMPRESSED_OK.replace("{{TASK_DESCRIPTION}}", "the task")
        verdict = validate_placeholders(TEMPLATE, tampered)
        assert not verdict.passed
        assert "TASK_DESCRIPTION" in verdict.detail

    def test_dropped_conditional_marker_is_rejected(self) -> None:
        tampered = COMPRESSED_OK.replace("{{#IF FILES}}\n", "").replace("{{/IF}}\n", "")
        assert not validate_placeholders(TEMPLATE, tampered).passed

    def test_url_set_check_ignores_repeated_mentions(self) -> None:
        doubled = TEMPLATE + "\nAgain: https://docs.example.test/style\n"
        assert validate_urls(doubled, TEMPLATE).passed


class TestCompletionBlockExtraction:
    def test_extracts_expanded_block_to_next_same_level_heading(self) -> None:
        block = extract_completion_block(EXPANDED_TEMPLATE)
        assert block is not None
        assert block.startswith("## Done signal (completion contract worker-completion/v1)")
        assert "curl -s -X POST" in block
        assert "## Bulletin board" not in block

    def test_absent_block_returns_none(self) -> None:
        assert extract_completion_block(TEMPLATE) is None

    def test_matches_any_contract_version(self) -> None:
        bumped = EXPANDED_TEMPLATE.replace("worker-completion/v1", "worker-completion/v2")
        assert extract_completion_block(bumped) is not None


# ---------------------------------------------------------------------------
# Targeted fix retry loop (max 2)
# ---------------------------------------------------------------------------


class TestFixRetryLoop:
    def test_passing_candidate_needs_no_fix(self) -> None:
        outcome = validate_template_rewrite(TEMPLATE, COMPRESSED_OK, fix_call=None)
        assert outcome.passed
        assert not outcome.aborted
        assert outcome.retry_count == 0
        assert outcome.text == COMPRESSED_OK

    def test_failure_without_fix_call_aborts(self) -> None:
        bad = COMPRESSED_OK.replace("{{INCLUDE completion_contract}}\n", "")
        outcome = validate_template_rewrite(TEMPLATE, bad, fix_call=None)
        assert outcome.aborted
        assert outcome.retry_count == 0

    def test_fix_succeeds_on_second_retry(self) -> None:
        bad = COMPRESSED_OK.replace("{{INCLUDE completion_contract}}\n", "")
        still_bad = COMPRESSED_OK.replace("## Verify", "## Check")
        responses = iter([still_bad, COMPRESSED_OK])

        prompts: list[str] = []

        def fix_call(prompt: str) -> str:
            prompts.append(prompt)
            return next(responses)

        outcome = validate_template_rewrite(TEMPLATE, bad, fix_call=fix_call)
        assert outcome.passed
        assert outcome.retry_count == 2
        assert outcome.text == COMPRESSED_OK
        # The fix prompt carries the original as the byte reference.
        assert TEMPLATE in prompts[0]

    def test_aborts_after_two_failed_retries(self) -> None:
        bad = COMPRESSED_OK.replace("{{INCLUDE completion_contract}}\n", "")
        calls = 0

        def fix_call(prompt: str) -> str:
            nonlocal calls
            calls += 1
            return bad

        outcome = validate_template_rewrite(TEMPLATE, bad, fix_call=fix_call)
        assert outcome.aborted
        assert not outcome.passed
        assert calls == TEMPLATE_MAX_FIX_RETRIES == 2
        assert outcome.retry_count == 2

    def test_fix_call_exception_aborts(self) -> None:
        bad = COMPRESSED_OK.replace("{{INCLUDE completion_contract}}\n", "")

        def fix_call(prompt: str) -> str:
            raise RuntimeError("adapter down")

        outcome = validate_template_rewrite(TEMPLATE, bad, fix_call=fix_call)
        assert outcome.aborted


class TestDeterminism:
    @pytest.mark.parametrize("post", [COMPRESSED_OK, TEMPLATE, EXPANDED_TEMPLATE])
    def test_same_pair_yields_identical_verdicts(self, post: str) -> None:
        first = run_template_validators(TEMPLATE, post)
        second = run_template_validators(TEMPLATE, post)
        assert first == second
