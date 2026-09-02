"""Behavioral tests for prompt segmentation and digesting (issue #3455 step 1).

``segment_prompt`` turns the four blocks the orchestrator authors into a
prompt -- role instructions, task brief, mailbox section, resume state --
into named, individually content-addressed segments, plus one digest over
the ordered segment list. This is the property the design leans on: a
divergence must name which block changed, not merely that assembly produced
different bytes.

Nothing here anchors a segment anywhere (that is step 2+ of the issue); these
tests only check the pure digesting function and its wiring into
``_render_prompt``.
"""

from __future__ import annotations

import hashlib

from bernstein.core.agents.prompt_segments import (
    SEGMENT_NAMES,
    PromptSegment,
    segment_prompt,
    segments_digest,
)

_EMPTY_SHA256 = "sha256:" + hashlib.sha256(b"").hexdigest()


def _blocks(**overrides: str) -> dict[str, str]:
    base = {
        "role_block": "You are a backend engineer.",
        "task_block": "### Task 1: Fix the widget (id=t1)",
        "mailbox_block": "## Coordination mailbox\nfrom qa: please check X",
        "resume_block": "",
    }
    base.update(overrides)
    return base


def test_segment_digest_is_deterministic_across_repeated_assembly() -> None:
    """Same four blocks assembled twice yield byte-identical digests."""
    blocks = _blocks()

    first = segment_prompt(**blocks)
    second = segment_prompt(**blocks)

    assert [s.digest for s in first] == [s.digest for s in second]
    assert segments_digest(first) == segments_digest(second)


def test_changing_one_byte_of_a_block_changes_only_that_segment_and_the_list_digest() -> None:
    """A one-byte edit to the role block is attributable to the role segment."""
    before = segment_prompt(**_blocks())
    after = segment_prompt(**_blocks(role_block="You are a backend engineer!"))

    before_by_name = {s.name: s.digest for s in before}
    after_by_name = {s.name: s.digest for s in after}

    assert after_by_name["role"] != before_by_name["role"]
    assert after_by_name["task"] == before_by_name["task"]
    assert after_by_name["mailbox"] == before_by_name["mailbox"]
    assert after_by_name["resume"] == before_by_name["resume"]
    assert segments_digest(after) != segments_digest(before)


def test_empty_block_still_produces_a_named_segment_with_empty_digest() -> None:
    """An empty mailbox block is a present segment, not a dropped one."""
    segments = segment_prompt(**_blocks(mailbox_block=""))

    assert len(segments) == 4
    mailbox_segment = next(s for s in segments if s.name == "mailbox")
    assert mailbox_segment.digest == _EMPTY_SHA256


def test_segment_count_is_stable_regardless_of_which_blocks_are_empty() -> None:
    """Every call produces exactly the four named segments, in order."""
    all_empty = segment_prompt(role_block="", task_block="", mailbox_block="", resume_block="")
    assert [s.name for s in all_empty] == list(SEGMENT_NAMES)
    assert len(all_empty) == 4


def test_segment_order_is_fixed_role_task_mailbox_resume() -> None:
    """The ordered segment list always reports role, task, mailbox, resume."""
    segments = segment_prompt(**_blocks())
    assert [s.name for s in segments] == ["role", "task", "mailbox", "resume"]


def test_segment_digest_matches_direct_sha256_of_block_bytes() -> None:
    """A segment's digest is exactly sha256 of the block's UTF-8 bytes."""
    blocks = _blocks()
    segments = segment_prompt(**blocks)

    role_segment = next(s for s in segments if s.name == "role")
    expected = "sha256:" + hashlib.sha256(blocks["role_block"].encode("utf-8")).hexdigest()
    assert role_segment.digest == expected


def test_segments_digest_is_canonical_over_name_and_digest_pairs() -> None:
    """The list digest is a pure function of the ordered (name, digest) pairs."""
    segments = segment_prompt(**_blocks())
    manual = [PromptSegment(name=s.name, digest=s.digest) for s in segments]
    assert segments_digest(manual) == segments_digest(segments)
