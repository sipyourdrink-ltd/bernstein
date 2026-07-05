"""Fixture suite for the zero-LLM compaction validators (issue #2246).

Covers:
- Fence extraction: backtick fences, tilde fences, nested fences,
  indented (1-3 space) fences, unclosed fences.
- validate_code_blocks: byte-equal or dropped whole; rewrites,
  truncations, and partial retention rejected (adversarial fixtures).
- validate_quoted_errors: quoted error strings preserved verbatim.
- validate_failed_actions: tagged failed-action blocks retained intact.
- validate_file_paths: no invented paths; retained-section paths present.
- validate_pinned_messages: pinned meta-messages preserved.
- Purity: the same (pre, post) pair yields identical verdicts on every
  call (AC #1).
- validate_with_fix: fix-only pass, max 1 retry, then abort (reactive
  fallback stays with the caller).
"""

from __future__ import annotations

from bernstein.core.tokens.compaction_validate import (
    MAX_FIX_RETRIES,
    PINNED_PREFIX,
    VALIDATOR_NAMES,
    ValidatorVerdict,
    all_passed,
    build_fix_prompt,
    extract_fenced_blocks,
    run_validators,
    validate_code_blocks,
    validate_failed_actions,
    validate_file_paths,
    validate_pinned_messages,
    validate_quoted_errors,
    validate_with_fix,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_CODE_BLOCK = 'def handler(x):\n    return x * 2\n\nprint("done")'

_PRE_WITH_CODE = f"""Intro narrative about the task.

```python
{_CODE_BLOCK}
```

Closing narrative.
"""


def _post_with_code(block: str) -> str:
    return f"""Summary of the task.

```python
{block}
```
"""


# ---------------------------------------------------------------------------
# Fence extraction
# ---------------------------------------------------------------------------


class TestExtractFencedBlocks:
    def test_extracts_backtick_block(self) -> None:
        blocks = extract_fenced_blocks(_PRE_WITH_CODE)
        assert len(blocks) == 1
        assert blocks[0].content == _CODE_BLOCK
        assert blocks[0].info == "python"

    def test_extracts_tilde_block(self) -> None:
        text = "before\n~~~sh\necho hi\n~~~\nafter\n"
        blocks = extract_fenced_blocks(text)
        assert len(blocks) == 1
        assert blocks[0].content == "echo hi"
        assert blocks[0].fence_char == "~"

    def test_nested_fence_stays_inside_outer_block(self) -> None:
        inner = "```py\nprint(1)\n```"
        text = f"````md\n{inner}\n````\n"
        blocks = extract_fenced_blocks(text)
        assert len(blocks) == 1
        assert blocks[0].content == inner

    def test_tilde_fence_inside_backtick_block_does_not_close_it(self) -> None:
        text = "```txt\n~~~\nstill inside\n~~~\n```\n"
        blocks = extract_fenced_blocks(text)
        assert len(blocks) == 1
        assert blocks[0].content == "~~~\nstill inside\n~~~"

    def test_shorter_close_fence_does_not_close(self) -> None:
        text = "````\n```\ninner\n````\ntail\n"
        blocks = extract_fenced_blocks(text)
        assert len(blocks) == 1
        assert blocks[0].content == "```\ninner"

    def test_indented_fence_up_to_three_spaces_recognised(self) -> None:
        text = "para\n\n   ```\n   code line\n   ```\n"
        blocks = extract_fenced_blocks(text)
        assert len(blocks) == 1
        assert blocks[0].content == "   code line"

    def test_four_space_indent_is_not_a_fence(self) -> None:
        text = "para\n\n    ```\n    not a fence\n    ```\n"
        assert extract_fenced_blocks(text) == []

    def test_unclosed_fence_runs_to_end(self) -> None:
        text = "start\n```\ndangling code"
        blocks = extract_fenced_blocks(text)
        assert len(blocks) == 1
        assert blocks[0].content == "dangling code"

    def test_two_backticks_is_not_a_fence(self) -> None:
        assert extract_fenced_blocks("``\nnot code\n``\n") == []


# ---------------------------------------------------------------------------
# validate_code_blocks (AC #3 adversarial fixtures)
# ---------------------------------------------------------------------------


class TestValidateCodeBlocks:
    def test_byte_equal_block_passes(self) -> None:
        verdict = validate_code_blocks(_PRE_WITH_CODE, _post_with_code(_CODE_BLOCK))
        assert verdict.passed

    def test_rewritten_block_rejected(self) -> None:
        rewritten = _CODE_BLOCK.replace("x * 2", "x * 3")
        verdict = validate_code_blocks(_PRE_WITH_CODE, _post_with_code(rewritten))
        assert not verdict.passed
        assert "rewritten" in verdict.detail or "not present" in verdict.detail

    def test_truncated_block_rejected(self) -> None:
        truncated = _CODE_BLOCK.splitlines()[0]
        verdict = validate_code_blocks(_PRE_WITH_CODE, _post_with_code(truncated))
        assert not verdict.passed

    def test_whitespace_change_rejected(self) -> None:
        reindented = _CODE_BLOCK.replace("    return", "  return")
        verdict = validate_code_blocks(_PRE_WITH_CODE, _post_with_code(reindented))
        assert not verdict.passed

    def test_block_dropped_whole_passes(self) -> None:
        post = "Summary of the task with the code removed entirely."
        verdict = validate_code_blocks(_PRE_WITH_CODE, post)
        assert verdict.passed

    def test_partial_retention_outside_fence_rejected(self) -> None:
        # A fragment of a dropped block leaks into the prose: not whole.
        post = "Summary. The code did def handler(x):\n    return x * 2 somewhere."
        verdict = validate_code_blocks(_PRE_WITH_CODE, post)
        assert not verdict.passed

    def test_invented_block_rejected(self) -> None:
        post = _post_with_code("rm -rf /tmp/everything")
        verdict = validate_code_blocks("no code here", post)
        assert not verdict.passed

    def test_duplicated_block_rejected(self) -> None:
        post = _post_with_code(_CODE_BLOCK) + "\n" + _post_with_code(_CODE_BLOCK)
        verdict = validate_code_blocks(_PRE_WITH_CODE, post)
        assert not verdict.passed

    def test_tilde_fenced_block_round_trips(self) -> None:
        pre = "intro\n~~~toml\nkey = 1\n~~~\n"
        post = "summary\n~~~toml\nkey = 1\n~~~\n"
        assert validate_code_blocks(pre, post).passed

    def test_nested_fence_content_must_match_exactly(self) -> None:
        inner = "```py\nprint(1)\n```"
        pre = f"````md\n{inner}\n````\n"
        post = "````md\n```py\nprint(2)\n```\n````\n"
        assert not validate_code_blocks(pre, post).passed


# ---------------------------------------------------------------------------
# validate_quoted_errors
# ---------------------------------------------------------------------------


class TestValidateQuotedErrors:
    def test_quoted_error_preserved_passes(self) -> None:
        pre = 'The run failed with "ValueError: bad input on line 3" earlier.'
        post = 'Summary: hit "ValueError: bad input on line 3", then stopped.'
        assert validate_quoted_errors(pre, post).passed

    def test_quoted_error_dropped_fails(self) -> None:
        pre = 'The run failed with "ValueError: bad input on line 3" earlier.'
        post = "Summary: a value error happened."
        verdict = validate_quoted_errors(pre, post)
        assert not verdict.passed
        assert "ValueError" in verdict.detail

    def test_quoted_error_paraphrased_fails(self) -> None:
        pre = "Saw `TimeoutError: deadline exceeded after 30s` in the log."
        post = "Saw `TimeoutError: deadline exceeded` in the log."
        assert not validate_quoted_errors(pre, post).passed

    def test_non_error_quotes_are_not_required(self) -> None:
        pre = 'The plan says "refactor the parser" next.'
        post = "Summary without the quote."
        assert validate_quoted_errors(pre, post).passed

    def test_traceback_quote_required(self) -> None:
        pre = "Log had 'Traceback (most recent call last)' twice."
        post = "Log had problems."
        assert not validate_quoted_errors(pre, post).passed


# ---------------------------------------------------------------------------
# validate_failed_actions
# ---------------------------------------------------------------------------

_FAILED_BLOCK = "[FAILED -- kept for reference] tool=run_tests turn=2 staleness=0\nAssertionError: expected 3 got 4"


class TestValidateFailedActions:
    def test_retained_block_preserved_passes(self) -> None:
        pre = f"scrollback\n\n{_FAILED_BLOCK}\n\ntail"
        post = f"summary\n\n{_FAILED_BLOCK}\n"
        assert validate_failed_actions(pre, post).passed

    def test_retained_block_dropped_fails(self) -> None:
        pre = f"scrollback\n\n{_FAILED_BLOCK}\n\ntail"
        post = "summary without the failure record"
        verdict = validate_failed_actions(pre, post)
        assert not verdict.passed
        assert "run_tests" in verdict.detail

    def test_retained_block_edited_fails(self) -> None:
        pre = f"scrollback\n\n{_FAILED_BLOCK}\n\ntail"
        post = f"summary\n\n{_FAILED_BLOCK.replace('got 4', 'got 5')}\n"
        assert not validate_failed_actions(pre, post).passed

    def test_no_retained_blocks_passes(self) -> None:
        assert validate_failed_actions("plain text", "summary").passed


# ---------------------------------------------------------------------------
# validate_file_paths
# ---------------------------------------------------------------------------


class TestValidateFilePaths:
    def test_paths_in_retained_sections_must_survive(self) -> None:
        pre = f"intro\n\n{PINNED_PREFIX} check src/pkg/module.py before merging\n\ntail"
        post = "summary without the path"
        verdict = validate_file_paths(pre, post)
        assert not verdict.passed
        assert "src/pkg/module.py" in verdict.detail

    def test_paths_in_retained_sections_present_passes(self) -> None:
        pre = f"intro\n\n{PINNED_PREFIX} check src/pkg/module.py before merging\n\ntail"
        post = f"summary\n\n{PINNED_PREFIX} check src/pkg/module.py before merging\n"
        assert validate_file_paths(pre, post).passed

    def test_invented_path_in_post_fails(self) -> None:
        pre = "we edited src/app/real.py earlier"
        post = "we edited src/app/imaginary_helper.py earlier"
        verdict = validate_file_paths(pre, post)
        assert not verdict.passed
        assert "imaginary_helper" in verdict.detail

    def test_dropped_narrative_path_is_allowed(self) -> None:
        pre = "we touched src/app/real.py and src/app/other.py"
        post = "we touched src/app/real.py"
        assert validate_file_paths(pre, post).passed


# ---------------------------------------------------------------------------
# validate_pinned_messages
# ---------------------------------------------------------------------------


class TestValidatePinnedMessages:
    def test_pinned_line_preserved_passes(self) -> None:
        pre = f"top\n{PINNED_PREFIX} never touch the migrations folder\nrest"
        post = f"summary\n{PINNED_PREFIX} never touch the migrations folder\n"
        assert validate_pinned_messages(pre, post).passed

    def test_pinned_line_dropped_fails(self) -> None:
        pre = f"top\n{PINNED_PREFIX} never touch the migrations folder\nrest"
        post = "summary only"
        verdict = validate_pinned_messages(pre, post)
        assert not verdict.passed
        assert "migrations" in verdict.detail

    def test_pinned_line_reworded_fails(self) -> None:
        pre = f"top\n{PINNED_PREFIX} never touch the migrations folder\nrest"
        post = f"summary\n{PINNED_PREFIX} avoid the migrations folder\n"
        assert not validate_pinned_messages(pre, post).passed

    def test_no_pins_passes(self) -> None:
        assert validate_pinned_messages("plain", "summary").passed


# ---------------------------------------------------------------------------
# run_validators: determinism / purity (AC #1)
# ---------------------------------------------------------------------------


class TestRunValidators:
    def test_names_are_stable_and_ordered(self) -> None:
        verdicts = run_validators("pre", "post")
        assert tuple(v.name for v in verdicts) == VALIDATOR_NAMES

    def test_same_pair_yields_identical_verdicts(self) -> None:
        pre = f"{_PRE_WITH_CODE}\n{_FAILED_BLOCK}\n\n{PINNED_PREFIX} keep src/a/b.py\n"
        post = "a mangled summary with `SomeError: x` missing"
        first = run_validators(pre, post)
        for _ in range(3):
            assert run_validators(pre, post) == first

    def test_all_passed_helper(self) -> None:
        good = (ValidatorVerdict(name="x", passed=True),)
        bad = (ValidatorVerdict(name="x", passed=True), ValidatorVerdict(name="y", passed=False))
        assert all_passed(good)
        assert not all_passed(bad)

    def test_clean_summary_passes_everything(self) -> None:
        pre = "Long narrative about work done on src/app/real.py.\n"
        post = "Short narrative about src/app/real.py."
        assert all_passed(run_validators(pre, post))


# ---------------------------------------------------------------------------
# Fix pass: fix-only prompt, max 1 retry, then abort
# ---------------------------------------------------------------------------


class TestValidateWithFix:
    def test_passes_first_time_without_fix_call(self) -> None:
        outcome = validate_with_fix("original text", "clean summary")
        assert outcome.passed
        assert not outcome.aborted
        assert outcome.retry_count == 0
        assert outcome.text == "clean summary"

    def test_fix_pass_repairs_summary(self) -> None:
        pre = _PRE_WITH_CODE
        bad_post = _post_with_code(_CODE_BLOCK.replace("x * 2", "x * 9"))
        good_post = _post_with_code(_CODE_BLOCK)

        calls: list[str] = []

        def fix_call(prompt: str) -> str:
            calls.append(prompt)
            return good_post

        outcome = validate_with_fix(pre, bad_post, fix_call=fix_call)
        assert outcome.passed
        assert outcome.retry_count == 1
        assert outcome.text == good_post
        assert len(calls) == 1

    def test_fix_prompt_is_fix_only_and_carries_original(self) -> None:
        pre = _PRE_WITH_CODE
        bad_post = _post_with_code("mangled()")
        seen: list[str] = []

        def fix_call(prompt: str) -> str:
            seen.append(prompt)
            return bad_post  # does not fix anything

        validate_with_fix(pre, bad_post, fix_call=fix_call)
        assert len(seen) == 1
        prompt = seen[0]
        assert "Do NOT re-summarize" in prompt
        assert pre in prompt  # original supplied as reference
        assert bad_post in prompt

    def test_aborts_after_max_one_retry(self) -> None:
        pre = _PRE_WITH_CODE
        bad_post = _post_with_code("mangled()")
        calls: list[str] = []

        def fix_call(prompt: str) -> str:
            calls.append(prompt)
            return _post_with_code("still mangled()")

        outcome = validate_with_fix(pre, bad_post, fix_call=fix_call)
        assert not outcome.passed
        assert outcome.aborted
        assert outcome.retry_count == MAX_FIX_RETRIES == 1
        assert len(calls) == 1

    def test_aborts_immediately_without_fix_call(self) -> None:
        pre = _PRE_WITH_CODE
        bad_post = _post_with_code("mangled()")
        outcome = validate_with_fix(pre, bad_post)
        assert not outcome.passed
        assert outcome.aborted
        assert outcome.retry_count == 0

    def test_fix_call_exception_aborts(self) -> None:
        pre = _PRE_WITH_CODE
        bad_post = _post_with_code("mangled()")

        def fix_call(prompt: str) -> str:
            raise RuntimeError("provider down")

        outcome = validate_with_fix(pre, bad_post, fix_call=fix_call)
        assert not outcome.passed
        assert outcome.aborted

    def test_build_fix_prompt_names_failed_validators(self) -> None:
        verdicts = (
            ValidatorVerdict(name="code_blocks", passed=False, detail="block rewritten"),
            ValidatorVerdict(name="pinned_messages", passed=True),
        )
        prompt = build_fix_prompt("orig", "summ", verdicts)
        assert "code_blocks" in prompt
        assert "block rewritten" in prompt
        assert "pinned_messages" not in prompt
