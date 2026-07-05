"""Zero-LLM mechanical validators for compaction summaries (issue #2246).

A compacted context is a state-mutating artifact: once it replaces the
original, whatever the summary dropped or rewrote is gone for the rest of
the run. These validators are the mechanical gate between the summary
stage and the worker. Every validator is a pure function over the
``(pre, post)`` string pair - no LLM, no IO, no clock - so the same pair
yields identical verdicts on every host, every run (AC #1).

Validators (run in this fixed order by :func:`run_validators`):

``code_blocks``
    Every fenced code block in *post* must be byte-equal to a block in
    *pre* (multiset semantics - no invented, rewritten, truncated, or
    duplicated blocks). A *pre* block may be dropped, but only whole:
    if any line of a dropped block leaks into *post* outside a fence,
    the block was partially retained and the summary is rejected.
    Fence handling follows the CommonMark rules that matter for byte
    fidelity: backtick and tilde fences, closing fences of equal or
    greater length and the same character (so nested shorter or
    other-character fences stay inside the block), fences indented up
    to three spaces, and unclosed fences running to end of text.

``quoted_errors``
    Quoted strings in *pre* that carry an error marker (``Error``,
    ``Exception``, ``Traceback``, ``FATAL``, ``errno``) must appear
    verbatim in *post*. Error text is evidence; paraphrase destroys it.

``failed_actions``
    Retained failed-action blocks (the ``[FAILED -- kept for
    reference]`` records from
    :mod:`bernstein.core.tokens.failed_action_retention`) must survive
    byte-equal.

``file_paths``
    Two directions: every path-like token in *post* must already exist
    in *pre* (no invented paths), and paths inside *pre*'s retained
    sections (failed-action blocks and pinned lines) must still be
    present in *post*.

``pinned_messages``
    Lines starting with :data:`PINNED_PREFIX` must survive byte-equal.

On validator failure the caller may run exactly one targeted fix pass
(:func:`validate_with_fix`): the fix prompt forbids re-summarizing,
supplies the original as reference, and is retried at most
:data:`MAX_FIX_RETRIES` (= 1) time before the compaction is aborted so
the caller falls back to the reactive path unchanged.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from bernstein.core.tokens.failed_action_retention import split_retained_blocks

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Marker for operator/meta messages that must survive compaction verbatim.
#: Plain ASCII, two hyphens nowhere - grep-stable across encodings.
PINNED_PREFIX: Final[str] = "[PINNED]"

#: Validator names in the exact order :func:`run_validators` executes them.
VALIDATOR_NAMES: Final[tuple[str, ...]] = (
    "code_blocks",
    "quoted_errors",
    "failed_actions",
    "file_paths",
    "pinned_messages",
)

#: Maximum number of targeted fix passes before compaction is aborted.
MAX_FIX_RETRIES: Final[int] = 1

#: Minimum stripped-line length considered when checking whether a dropped
#: code block leaked a fragment into the summary prose. Short lines
#: (``}``, ``end``, ``pass``) are too common in narrative text to be
#: reliable evidence of partial retention.
_FRAGMENT_MIN_LEN: Final[int] = 12

#: Error markers that make a quoted string load-bearing. Matched
#: case-sensitively: these are the spellings Python/JS/Go tracebacks and
#: loggers actually emit, and case-folding would drag in prose like
#: "there was an error".
_ERROR_MARKERS: Final[tuple[str, ...]] = (
    "Error",
    "Exception",
    "Traceback",
    "FATAL",
    "errno",
)

# Fence line: up to 3 spaces of indent, then >= 3 backticks or tildes,
# then an optional info string. CommonMark forbids backticks in the info
# string of a backtick fence (that is inline code, not a fence).
_FENCE_OPEN_RE: Final[re.Pattern[str]] = re.compile(r"^( {0,3})(`{3,}|~{3,})[ \t]*([^\n`]*)$")
_FENCE_CLOSE_RE_TMPL: Final[str] = r"^ {{0,3}}{char}{{{min_len},}}[ \t]*$"

# Path-like token: at least one slash-separated segment, e.g.
# ``src/pkg/module.py``, ``./a/b``, ``/tmp/x.log``. Trailing punctuation
# is excluded so prose like "(see src/a/b.py)." extracts cleanly.
_PATH_RE: Final[re.Pattern[str]] = re.compile(r"(?:\.{1,2}/|/)?[\w.-]+(?:/[\w.-]+)+")

# Quoted spans: double quotes, single quotes, or backticks; single line,
# bounded length so pathological inputs cannot blow up extraction.
_QUOTE_RES: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r'"([^"\n]{4,400})"'),
    re.compile(r"'([^'\n]{4,400})'"),
    re.compile(r"`([^`\n]{4,400})`"),
)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FencedBlock:
    """One fenced code block extracted from a context string.

    Attributes:
        content: The exact bytes between the fence lines (no trailing
            newline). This is the identity used for byte-equality checks.
        info: The fence info string (e.g. ``python``), stripped.
        fence_char: ``"`"`` or ``"~"``.
        start_line: 1-based line number of the opening fence.
    """

    content: str
    info: str
    fence_char: str
    start_line: int


@dataclass(frozen=True, slots=True)
class ValidatorVerdict:
    """Outcome of one validator over a ``(pre, post)`` pair.

    Attributes:
        name: Validator name (one of :data:`VALIDATOR_NAMES`).
        passed: Whether the summary satisfied the invariant.
        detail: Human-readable evidence for a failure; empty on pass.
    """

    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ValidationOutcome:
    """Result of :func:`validate_with_fix`.

    Attributes:
        text: The summary text that should reach the caller: the fixed
            text when the fix pass repaired it, otherwise the last
            candidate that was validated. Callers MUST check ``passed``
            before using it - an aborted outcome's text never reaches
            the worker.
        verdicts: Verdicts for the final validated candidate.
        retry_count: Number of fix passes executed (0 or 1).
        passed: Whether the final candidate passed every validator.
        aborted: True when validation failed after the retry budget;
            the caller must abort compaction and fall back to the
            reactive path unchanged.
    """

    text: str
    verdicts: tuple[ValidatorVerdict, ...]
    retry_count: int
    passed: bool
    aborted: bool


# ---------------------------------------------------------------------------
# Fence extraction
# ---------------------------------------------------------------------------


def extract_fenced_blocks(text: str) -> list[FencedBlock]:
    """Extract fenced code blocks with CommonMark open/close semantics.

    Args:
        text: The context string to scan.

    Returns:
        Blocks in document order. An unclosed fence yields a block
        running to the end of *text*.
    """
    blocks: list[FencedBlock] = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        match = _FENCE_OPEN_RE.match(lines[i])
        if match is None:
            i += 1
            continue
        fence = match.group(2)
        fence_char = fence[0]
        info = match.group(3).strip()
        # Tilde fences may carry any info string; backtick fences must not
        # contain a backtick in it (enforced by the regex already).
        close_re = re.compile(_FENCE_CLOSE_RE_TMPL.format(char=re.escape(fence_char), min_len=len(fence)))
        start_line = i + 1
        j = i + 1
        content_lines: list[str] = []
        while j < len(lines) and close_re.match(lines[j]) is None:
            content_lines.append(lines[j])
            j += 1
        blocks.append(
            FencedBlock(
                content="\n".join(content_lines),
                info=info,
                fence_char=fence_char,
                start_line=start_line,
            )
        )
        # Skip past the closing fence when present; an unclosed fence
        # consumed everything, so either way resume after ``j``.
        i = j + 1
    return blocks


# ---------------------------------------------------------------------------
# Individual validators (pure functions)
# ---------------------------------------------------------------------------


def validate_code_blocks(pre: str, post: str) -> ValidatorVerdict:
    """Fenced code blocks are byte-equal or dropped whole - never rewritten.

    Args:
        pre: Context before compaction.
        post: Candidate compacted context.

    Returns:
        A ``code_blocks`` verdict. Fails when *post* contains a block
        absent from *pre* (invented / rewritten / truncated / duplicated)
        or when a dropped *pre* block leaks a fragment into *post*.
    """
    pre_blocks = extract_fenced_blocks(pre)
    post_blocks = extract_fenced_blocks(post)
    budget = Counter(block.content for block in pre_blocks)

    for block in post_blocks:
        if budget[block.content] <= 0:
            snippet = block.content[:80]
            return ValidatorVerdict(
                name="code_blocks",
                passed=False,
                detail=(
                    f"fenced block at post line {block.start_line} is not present "
                    f"byte-equal in the original (rewritten, truncated, or invented): {snippet!r}"
                ),
            )
        budget[block.content] -= 1

    # Dropped blocks must be dropped whole: no fragment may survive in the
    # post prose. Only lines long enough to be unambiguous count.
    retained = {block.content for block in post_blocks}
    post_text_outside = post
    for content in retained:
        post_text_outside = post_text_outside.replace(content, "")
    for block in pre_blocks:
        if block.content in retained:
            continue
        for line in block.content.split("\n"):
            stripped = line.strip()
            if len(stripped) >= _FRAGMENT_MIN_LEN and stripped in post_text_outside:
                return ValidatorVerdict(
                    name="code_blocks",
                    passed=False,
                    detail=(
                        f"dropped fenced block (pre line {block.start_line}) leaked a "
                        f"fragment into the summary: {stripped[:80]!r}"
                    ),
                )
    return ValidatorVerdict(name="code_blocks", passed=True)


def _extract_error_quotes(text: str) -> list[str]:
    """Return quoted spans in *text* that carry an error marker."""
    found: list[str] = []
    for pattern in _QUOTE_RES:
        for match in pattern.finditer(text):
            span = match.group(1)
            if any(marker in span for marker in _ERROR_MARKERS):
                found.append(span)
    return found


def validate_quoted_errors(pre: str, post: str) -> ValidatorVerdict:
    """Quoted error strings from *pre* must appear verbatim in *post*.

    Args:
        pre: Context before compaction.
        post: Candidate compacted context.

    Returns:
        A ``quoted_errors`` verdict naming the first missing error span.
    """
    for span in _extract_error_quotes(pre):
        if span not in post:
            return ValidatorVerdict(
                name="quoted_errors",
                passed=False,
                detail=f"quoted error string missing from summary: {span[:120]!r}",
            )
    return ValidatorVerdict(name="quoted_errors", passed=True)


def validate_failed_actions(pre: str, post: str) -> ValidatorVerdict:
    """Retained failed-action blocks must survive compaction byte-equal.

    Args:
        pre: Context before compaction.
        post: Candidate compacted context.

    Returns:
        A ``failed_actions`` verdict naming the first missing block.
    """
    # rstrip: the extraction regex includes the trailing newline when a
    # block sits at end-of-text, which is a document artifact rather than
    # block content. Interior bytes stay byte-exact.
    _, pre_raw = split_retained_blocks(pre)
    _, post_raw = split_retained_blocks(post)
    pre_blocks = [block.rstrip() for block in pre_raw]
    budget = Counter(block.rstrip() for block in post_raw)
    for block in pre_blocks:
        if budget[block] <= 0:
            return ValidatorVerdict(
                name="failed_actions",
                passed=False,
                detail=f"retained failed-action block missing or edited: {block.splitlines()[0][:120]!r}",
            )
        budget[block] -= 1
    return ValidatorVerdict(name="failed_actions", passed=True)


def _pinned_lines(text: str) -> list[str]:
    """Return pinned lines (rstripped) in document order."""
    return [line.rstrip() for line in text.split("\n") if line.lstrip().startswith(PINNED_PREFIX)]


def _retained_sections(text: str) -> list[str]:
    """Return the sections of *text* the validators require to be retained."""
    _, failed_blocks = split_retained_blocks(text)
    return [*failed_blocks, *_pinned_lines(text)]


def validate_file_paths(pre: str, post: str) -> ValidatorVerdict:
    """Paths in retained sections survive; no path is invented.

    Args:
        pre: Context before compaction.
        post: Candidate compacted context.

    Returns:
        A ``file_paths`` verdict. Fails when *post* mentions a path that
        never appeared in *pre*, or when a path inside one of *pre*'s
        retained sections (failed-action blocks, pinned lines) is gone.
    """
    pre_paths = set(_PATH_RE.findall(pre))
    for path in _PATH_RE.findall(post):
        if path not in pre_paths:
            return ValidatorVerdict(
                name="file_paths",
                passed=False,
                detail=f"summary mentions a path absent from the original: {path!r}",
            )
    for section in _retained_sections(pre):
        for path in _PATH_RE.findall(section):
            if path not in post:
                return ValidatorVerdict(
                    name="file_paths",
                    passed=False,
                    detail=f"path from a retained section missing from summary: {path!r}",
                )
    return ValidatorVerdict(name="file_paths", passed=True)


def validate_pinned_messages(pre: str, post: str) -> ValidatorVerdict:
    """Pinned meta-messages must survive compaction verbatim.

    Args:
        pre: Context before compaction.
        post: Candidate compacted context.

    Returns:
        A ``pinned_messages`` verdict naming the first missing pin.
    """
    budget = Counter(_pinned_lines(post))
    for line in _pinned_lines(pre):
        if budget[line] <= 0:
            return ValidatorVerdict(
                name="pinned_messages",
                passed=False,
                detail=f"pinned message missing or reworded: {line[:120]!r}",
            )
        budget[line] -= 1
    return ValidatorVerdict(name="pinned_messages", passed=True)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

_VALIDATORS: Final[tuple[Callable[[str, str], ValidatorVerdict], ...]] = (
    validate_code_blocks,
    validate_quoted_errors,
    validate_failed_actions,
    validate_file_paths,
    validate_pinned_messages,
)


def run_validators(pre: str, post: str) -> tuple[ValidatorVerdict, ...]:
    """Run every validator over the pair, in :data:`VALIDATOR_NAMES` order.

    Args:
        pre: Context before compaction.
        post: Candidate compacted context.

    Returns:
        One verdict per validator; deterministic for a given pair.
    """
    return tuple(validator(pre, post) for validator in _VALIDATORS)


def all_passed(verdicts: Sequence[ValidatorVerdict]) -> bool:
    """Return True when every verdict passed."""
    return all(verdict.passed for verdict in verdicts)


# ---------------------------------------------------------------------------
# Targeted fix pass (fix-only; max one retry; abort otherwise)
# ---------------------------------------------------------------------------

_FIX_PROMPT_HEADER: Final[str] = (
    "The compacted context below failed mechanical validation.\n"
    "Repair ONLY the listed problems by copying the exact bytes from the\n"
    "ORIGINAL reference. Do NOT re-summarize, do NOT shorten further, do\n"
    "NOT reword anything that already validates. Return the full repaired\n"
    "compacted context and nothing else.\n"
)


def build_fix_prompt(
    original: str,
    summary: str,
    verdicts: Sequence[ValidatorVerdict],
) -> str:
    """Build the fix-only prompt for a failed validation.

    Args:
        original: The pre-compaction context (reference material).
        summary: The failing compacted candidate.
        verdicts: Verdicts from :func:`run_validators`; only failures are
            listed in the prompt.

    Returns:
        A prompt that forbids re-summarizing and carries the original as
        reference.
    """
    failures = "\n".join(f"- {v.name}: {v.detail}" for v in verdicts if not v.passed)
    return (
        f"{_FIX_PROMPT_HEADER}\n"
        f"Failed checks:\n{failures}\n\n"
        f"ORIGINAL (reference - copy exact bytes from here):\n"
        f"<<<ORIGINAL\n{original}\nORIGINAL>>>\n\n"
        f"COMPACTED (repair this):\n"
        f"<<<COMPACTED\n{summary}\nCOMPACTED>>>\n"
    )


def validate_with_fix(
    pre: str,
    post: str,
    *,
    fix_call: Callable[[str], str] | None = None,
) -> ValidationOutcome:
    """Validate *post*; on failure run at most one targeted fix pass.

    Args:
        pre: Context before compaction.
        post: Candidate compacted context.
        fix_call: Optional callable receiving the fix-only prompt and
            returning a repaired candidate. When ``None``, a validation
            failure aborts immediately (no fix pass is possible).

    Returns:
        A :class:`ValidationOutcome`. ``aborted=True`` means the caller
        must discard the compaction and fall back to the reactive path.
    """
    verdicts = run_validators(pre, post)
    if all_passed(verdicts):
        return ValidationOutcome(text=post, verdicts=verdicts, retry_count=0, passed=True, aborted=False)

    if fix_call is None:
        return ValidationOutcome(text=post, verdicts=verdicts, retry_count=0, passed=False, aborted=True)

    prompt = build_fix_prompt(pre, post, verdicts)
    try:
        fixed = fix_call(prompt)
    except Exception as exc:
        logger.warning("Compaction fix pass raised; aborting compaction: %s", exc)
        return ValidationOutcome(text=post, verdicts=verdicts, retry_count=MAX_FIX_RETRIES, passed=False, aborted=True)

    fixed_verdicts = run_validators(pre, fixed)
    if all_passed(fixed_verdicts):
        return ValidationOutcome(
            text=fixed,
            verdicts=fixed_verdicts,
            retry_count=MAX_FIX_RETRIES,
            passed=True,
            aborted=False,
        )
    return ValidationOutcome(
        text=fixed,
        verdicts=fixed_verdicts,
        retry_count=MAX_FIX_RETRIES,
        passed=False,
        aborted=True,
    )


__all__ = [
    "MAX_FIX_RETRIES",
    "PINNED_PREFIX",
    "VALIDATOR_NAMES",
    "FencedBlock",
    "ValidationOutcome",
    "ValidatorVerdict",
    "all_passed",
    "build_fix_prompt",
    "extract_fenced_blocks",
    "run_validators",
    "validate_code_blocks",
    "validate_failed_actions",
    "validate_file_paths",
    "validate_pinned_messages",
    "validate_quoted_errors",
    "validate_with_fix",
]
