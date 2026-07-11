"""Zero-LLM mechanical validators for role-template rewrites (issue #2249).

``bernstein templates compress`` sends a role prompt template to a model
for a token-economy rewrite. A template is a load-bearing artifact: every
worker spawn renders it, so a rewrite that drops a URL, rewords a fenced
command block, or touches the completion-payload instructions silently
breaks every subsequent spawn. These validators are the mechanical gate
between the rewrite stage and the on-disk template - pure functions over
the ``(pre, post)`` string pair, no LLM, no IO, no clock.

The base compaction validators from
:mod:`bernstein.core.tokens.compaction_validate` are reused unchanged
(fenced blocks byte-equal, quoted errors, retained sections, path and
pin preservation). Template-specific validators run after them, in this
fixed order:

``frontmatter``
    A leading YAML frontmatter block (``---`` fences) must be restored
    byte-equal. The compression engine splits it off before the rewrite
    and re-attaches it verbatim; this validator proves the round trip.

``headings``
    The full ATX heading sequence (outside fenced blocks) must survive
    byte-equal and in order. Compression shortens prose under headings,
    never the structural map itself.

``urls``
    The set of URLs must be preserved exactly: a dropped URL loses an
    instruction target, an invented URL is a hallucination.

``inline_code``
    The set of single-line inline-code spans (commands, flags, file
    names) must be preserved exactly.

``placeholders``
    Every ``{{...}}`` template token (placeholders, ``{{#IF}}`` blocks,
    ``{{INCLUDE}}`` directives) must be preserved as a multiset. The
    renderer contract depends on these bytes.

``completion_block``
    The completion-payload instruction block (worker completion
    contracts, #2244) is copied verbatim, never rewritten. Both forms
    are protected: the ``{{INCLUDE completion_contract}}`` directive
    line, and - for templates that inline the block - the expanded
    section starting at the completion-contract heading.

On validator failure the caller may run targeted fix passes
(:func:`validate_template_rewrite`): the fix prompt forbids
re-compressing and supplies the original as reference, retried at most
:data:`TEMPLATE_MAX_FIX_RETRIES` (= 2) times before the compression is
aborted and the original template is left untouched.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from typing import TYPE_CHECKING, Final

from bernstein.core.tokens.compaction_validate import (
    _FENCE_OPEN_RE,  # pyright: ignore[reportPrivateUsage]
    ValidationOutcome,
    ValidatorVerdict,
    all_passed,
    build_fix_prompt,
    run_validators,
)
from bernstein.core.tokens.compaction_validate import (
    VALIDATOR_NAMES as BASE_VALIDATOR_NAMES,
)

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Maximum number of targeted fix passes before a compression is aborted
#: and the original template restored (issue #2249 spec: max 2).
TEMPLATE_MAX_FIX_RETRIES: Final[int] = 2

#: Template-specific validator names, in run order (after the base ones).
TEMPLATE_VALIDATOR_NAMES: Final[tuple[str, ...]] = (
    "frontmatter",
    "headings",
    "urls",
    "inline_code",
    "placeholders",
    "completion_block",
)

#: Every validator :func:`run_template_validators` executes, in order.
ALL_TEMPLATE_VALIDATOR_NAMES: Final[tuple[str, ...]] = BASE_VALIDATOR_NAMES + TEMPLATE_VALIDATOR_NAMES

#: Heading line of the expanded completion-payload instruction block
#: (``templates/roles/_includes/completion_contract.md``). Matches any
#: contract version so a contract bump does not silently disarm the
#: validator.
_COMPLETION_HEADING_RE: Final[re.Pattern[str]] = re.compile(
    r"^## Done signal \(completion contract [\w./-]+\)\s*$",
)

#: ``{{INCLUDE name}}`` directive (same shape the renderer expands).
_INCLUDE_DIRECTIVE_RE: Final[re.Pattern[str]] = re.compile(r"\{\{INCLUDE\s+[\w-]+\}\}")

#: Any ``{{...}}`` template token: placeholders, conditional block
#: markers, include directives. Single line, non-greedy.
_TEMPLATE_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"\{\{[^{}\n]+\}\}")

#: URL shape: scheme plus non-space run, trailing punctuation excluded so
#: prose like "(see https://x.test/docs)." extracts cleanly.
_URL_RE: Final[re.Pattern[str]] = re.compile(r"https?://[^\s<>\"'`)\]]+[^\s<>\"'`)\].,;:]")

#: Single-line inline-code span. One backtick on each side; double
#: backticks and fence lines are excluded by construction (fence lines
#: are stripped from the prose view before extraction).
_INLINE_CODE_RE: Final[re.Pattern[str]] = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")

#: ATX heading: up to 3 spaces of indent, 1-6 ``#``, a space, then text.
_HEADING_RE: Final[re.Pattern[str]] = re.compile(r"^ {0,3}#{1,6} \S")

_FRONTMATTER_FENCE: Final[str] = "---"


# ---------------------------------------------------------------------------
# Structure helpers (pure)
# ---------------------------------------------------------------------------


def split_frontmatter(text: str) -> tuple[str, str]:
    """Split a leading YAML frontmatter block off *text*.

    A frontmatter block is a first line of exactly ``---`` followed by a
    later line of exactly ``---`` (both allowing trailing whitespace).

    Args:
        text: Full template text.

    Returns:
        ``(frontmatter, body)``. ``frontmatter`` includes both fence
        lines and the trailing newline of the closing fence; it is the
        empty string when *text* has no frontmatter, in which case
        ``body`` is *text* unchanged. ``frontmatter + body == text``
        always holds.
    """
    lines = text.split("\n")
    if not lines or lines[0].rstrip() != _FRONTMATTER_FENCE:
        return "", text
    for index in range(1, len(lines)):
        if lines[index].rstrip() == _FRONTMATTER_FENCE:
            frontmatter = "\n".join(lines[: index + 1]) + "\n"
            body = "\n".join(lines[index + 1 :])
            return frontmatter, body
    return "", text


def _prose_lines(text: str) -> list[str]:
    """Return the lines of *text* outside fenced code blocks.

    Fence handling mirrors :func:`~bernstein.core.tokens.
    compaction_validate.extract_fenced_blocks`: backtick and tilde
    fences, closing fences of equal or greater length and the same
    character, unclosed fences running to end of text. Fence lines
    themselves are excluded from the prose view.
    """
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        match = _FENCE_OPEN_RE.match(lines[i])
        if match is None:
            out.append(lines[i])
            i += 1
            continue
        fence = match.group(2)
        close_re = re.compile(rf"^ {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*$")
        j = i + 1
        while j < len(lines) and close_re.match(lines[j]) is None:
            j += 1
        i = j + 1
    return out


def extract_headings(text: str) -> list[str]:
    """Return ATX heading lines (rstripped) outside fenced blocks, in order."""
    return [line.rstrip() for line in _prose_lines(text) if _HEADING_RE.match(line)]


def extract_completion_block(text: str) -> str | None:
    """Return the expanded completion-instruction block from *text*, if any.

    The block starts at the completion-contract heading and runs to the
    next heading of the same or higher level (outside fenced blocks) or
    to end of text. Returns ``None`` when the heading is absent.
    """
    lines = text.split("\n")
    prose = set()
    # Mark which physical line indexes are prose (outside fences) so the
    # section scan cannot terminate on a "#" line inside a code block.
    i = 0
    while i < len(lines):
        match = _FENCE_OPEN_RE.match(lines[i])
        if match is None:
            prose.add(i)
            i += 1
            continue
        fence = match.group(2)
        close_re = re.compile(rf"^ {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*$")
        j = i + 1
        while j < len(lines) and close_re.match(lines[j]) is None:
            j += 1
        i = j + 1

    start: int | None = None
    for index, line in enumerate(lines):
        if index in prose and _COMPLETION_HEADING_RE.match(line):
            start = index
            break
    if start is None:
        return None
    level = lines[start].split(" ", 1)[0].count("#")
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if index not in prose:
            continue
        match = _HEADING_RE.match(lines[index])
        if match is None:
            continue
        heading_level = lines[index].lstrip().split(" ", 1)[0].count("#")
        if heading_level <= level:
            end = index
            break
    return "\n".join(lines[start:end])


# ---------------------------------------------------------------------------
# Template-specific validators (pure functions)
# ---------------------------------------------------------------------------


def validate_frontmatter(pre: str, post: str) -> ValidatorVerdict:
    """The leading YAML frontmatter block must be restored byte-equal.

    Args:
        pre: Template text before compression.
        post: Candidate compressed template.

    Returns:
        A ``frontmatter`` verdict; fails when the frontmatter was
        edited, dropped, or invented.
    """
    pre_front, _ = split_frontmatter(pre)
    post_front, _ = split_frontmatter(post)
    if pre_front != post_front:
        return ValidatorVerdict(
            name="frontmatter",
            passed=False,
            detail=(f"frontmatter must be restored verbatim: expected {pre_front[:80]!r}, got {post_front[:80]!r}"),
        )
    return ValidatorVerdict(name="frontmatter", passed=True)


def validate_headings(pre: str, post: str) -> ValidatorVerdict:
    """The ATX heading sequence must survive byte-equal and in order.

    Args:
        pre: Template text before compression.
        post: Candidate compressed template.

    Returns:
        A ``headings`` verdict naming the first diverging heading.
    """
    pre_headings = extract_headings(pre)
    post_headings = extract_headings(post)
    if pre_headings == post_headings:
        return ValidatorVerdict(name="headings", passed=True)
    for index, heading in enumerate(pre_headings):
        if index >= len(post_headings) or post_headings[index] != heading:
            found = post_headings[index] if index < len(post_headings) else "<missing>"
            return ValidatorVerdict(
                name="headings",
                passed=False,
                detail=f"heading #{index + 1} diverged: expected {heading!r}, got {found!r}",
            )
    return ValidatorVerdict(
        name="headings",
        passed=False,
        detail=f"rewrite invented heading(s): {post_headings[len(pre_headings)][:80]!r}",
    )


def validate_urls(pre: str, post: str) -> ValidatorVerdict:
    """The URL set must be preserved exactly - no drops, no inventions.

    Args:
        pre: Template text before compression.
        post: Candidate compressed template.

    Returns:
        A ``urls`` verdict naming the first missing or invented URL.
    """
    pre_urls = set(_URL_RE.findall(pre))
    post_urls = set(_URL_RE.findall(post))
    missing = sorted(pre_urls - post_urls)
    if missing:
        return ValidatorVerdict(
            name="urls",
            passed=False,
            detail=f"rewrite dropped URL: {missing[0]!r}",
        )
    invented = sorted(post_urls - pre_urls)
    if invented:
        return ValidatorVerdict(
            name="urls",
            passed=False,
            detail=f"rewrite invented URL: {invented[0]!r}",
        )
    return ValidatorVerdict(name="urls", passed=True)


def _inline_code_set(text: str) -> set[str]:
    """Return the set of inline-code spans in the prose of *text*."""
    prose = "\n".join(_prose_lines(text))
    return set(_INLINE_CODE_RE.findall(prose))


def validate_inline_code(pre: str, post: str) -> ValidatorVerdict:
    """The inline-code span set must be preserved exactly.

    Args:
        pre: Template text before compression.
        post: Candidate compressed template.

    Returns:
        An ``inline_code`` verdict naming the first missing or invented
        span.
    """
    pre_spans = _inline_code_set(pre)
    post_spans = _inline_code_set(post)
    missing = sorted(pre_spans - post_spans)
    if missing:
        return ValidatorVerdict(
            name="inline_code",
            passed=False,
            detail=f"rewrite dropped inline code span: {missing[0][:80]!r}",
        )
    invented = sorted(post_spans - pre_spans)
    if invented:
        return ValidatorVerdict(
            name="inline_code",
            passed=False,
            detail=f"rewrite invented inline code span: {invented[0][:80]!r}",
        )
    return ValidatorVerdict(name="inline_code", passed=True)


def validate_placeholders(pre: str, post: str) -> ValidatorVerdict:
    """Every ``{{...}}`` template token must be preserved as a multiset.

    Args:
        pre: Template text before compression.
        post: Candidate compressed template.

    Returns:
        A ``placeholders`` verdict naming the first missing or invented
        token.
    """
    pre_tokens = Counter(_TEMPLATE_TOKEN_RE.findall(pre))
    post_tokens = Counter(_TEMPLATE_TOKEN_RE.findall(post))
    if pre_tokens == post_tokens:
        return ValidatorVerdict(name="placeholders", passed=True)
    missing = sorted((pre_tokens - post_tokens).elements())
    if missing:
        return ValidatorVerdict(
            name="placeholders",
            passed=False,
            detail=f"rewrite dropped template token: {missing[0]!r}",
        )
    invented = sorted((post_tokens - pre_tokens).elements())
    return ValidatorVerdict(
        name="placeholders",
        passed=False,
        detail=f"rewrite invented template token: {invented[0]!r}",
    )


def validate_completion_block(pre: str, post: str) -> ValidatorVerdict:
    """The completion-payload instruction block is copied verbatim.

    Two forms are protected (worker completion contracts, #2244):

    * ``{{INCLUDE ...}}`` directive lines must survive as a multiset
      (byte-equal; the renderer expands them at spawn time).
    * When *pre* inlines the expanded block (completion-contract
      heading), the whole section must survive byte-equal.

    Args:
        pre: Template text before compression.
        post: Candidate compressed template.

    Returns:
        A ``completion_block`` verdict.
    """
    pre_includes = Counter(_INCLUDE_DIRECTIVE_RE.findall(pre))
    post_includes = Counter(_INCLUDE_DIRECTIVE_RE.findall(post))
    if pre_includes != post_includes:
        changed = sorted(set((pre_includes - post_includes) + (post_includes - pre_includes)))
        return ValidatorVerdict(
            name="completion_block",
            passed=False,
            detail=f"include directive altered or dropped: {changed[0]!r}",
        )

    pre_block = extract_completion_block(pre)
    if pre_block is not None:
        post_block = extract_completion_block(post)
        if post_block != pre_block:
            return ValidatorVerdict(
                name="completion_block",
                passed=False,
                detail="completion-payload instruction block was rewritten; it must be copied verbatim",
            )
    return ValidatorVerdict(name="completion_block", passed=True)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

_TEMPLATE_VALIDATORS: Final[tuple[Callable[[str, str], ValidatorVerdict], ...]] = (
    validate_frontmatter,
    validate_headings,
    validate_urls,
    validate_inline_code,
    validate_placeholders,
    validate_completion_block,
)


def run_template_validators(pre: str, post: str) -> tuple[ValidatorVerdict, ...]:
    """Run the base compaction validators plus the template-specific ones.

    Args:
        pre: Template text before compression.
        post: Candidate compressed template.

    Returns:
        One verdict per validator in
        :data:`ALL_TEMPLATE_VALIDATOR_NAMES` order; deterministic for a
        given pair.
    """
    base = run_validators(pre, post)
    extra = tuple(validator(pre, post) for validator in _TEMPLATE_VALIDATORS)
    return base + extra


def validate_template_rewrite(
    pre: str,
    post: str,
    *,
    fix_call: Callable[[str], str] | None = None,
    max_retries: int = TEMPLATE_MAX_FIX_RETRIES,
) -> ValidationOutcome:
    """Validate *post*; on failure run up to *max_retries* targeted fix passes.

    The fix prompt (reused from the compaction validators) forbids
    re-compressing and carries the original template as the byte
    reference. An outcome with ``aborted=True`` means the caller must
    leave the on-disk template untouched.

    Args:
        pre: Template text before compression.
        post: Candidate compressed template.
        fix_call: Optional callable receiving the fix-only prompt and
            returning a repaired candidate. ``None`` aborts on the
            first failure.
        max_retries: Maximum fix passes (default
            :data:`TEMPLATE_MAX_FIX_RETRIES`).

    Returns:
        A :class:`~bernstein.core.tokens.compaction_validate.
        ValidationOutcome` whose ``retry_count`` is the number of fix
        passes executed.
    """
    candidate = post
    verdicts = run_template_validators(pre, candidate)
    retries = 0
    while not all_passed(verdicts) and fix_call is not None and retries < max_retries:
        prompt = build_fix_prompt(pre, candidate, verdicts)
        retries += 1
        try:
            candidate = fix_call(prompt)
        except Exception as exc:
            logger.warning("Template compression fix pass raised; aborting: %s", exc)
            return ValidationOutcome(
                text=candidate,
                verdicts=verdicts,
                retry_count=retries,
                passed=False,
                aborted=True,
            )
        verdicts = run_template_validators(pre, candidate)

    passed = all_passed(verdicts)
    return ValidationOutcome(
        text=candidate,
        verdicts=verdicts,
        retry_count=retries,
        passed=passed,
        aborted=not passed,
    )


__all__ = [
    "ALL_TEMPLATE_VALIDATOR_NAMES",
    "TEMPLATE_MAX_FIX_RETRIES",
    "TEMPLATE_VALIDATOR_NAMES",
    "extract_completion_block",
    "extract_headings",
    "run_template_validators",
    "split_frontmatter",
    "validate_completion_block",
    "validate_frontmatter",
    "validate_headings",
    "validate_inline_code",
    "validate_placeholders",
    "validate_template_rewrite",
    "validate_urls",
]
