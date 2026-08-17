"""Untrusted issue text on its way to becoming an agent's prompt.

A volunteer task starts from an issue on a project the donor does not control.
:mod:`bernstein.core.volunteer.manifest` names the threat for the adjacent
case, gate commands: "The command originates in a repository the donor does not
control.  Handing attacker-influenceable text to a shell is the exfiltration
path this whole program exists to close."  The issue's own title and body are
the same category of input arriving at a different point -- they become the
``prompt`` argument at :meth:`~bernstein.adapters.base.CLIAdapter.spawn` -- and
nothing normalised them before this module existed.

There is no shell anywhere in that path, so escaping is not the job.  The job
is closing the gap between *what a reviewer read* and *what the model receives*,
and then framing what is left so its boundary cannot be moved from inside it.

Three channels close that gap, and only the third is the one people expect.

*Text the rendered page never showed.*  An HTML comment is where an instruction
hides in Markdown a reviewer skims.  Both spellings are stripped: a closed
``<!-- ... -->`` across any number of lines, and an unterminated ``<!--``, which
is not a stray tag -- it opens a CommonMark HTML block whose end condition is
never met, so the block runs to the end of the document and the rendered page
shows none of what follows while the API's raw body carries all of it.

The unterminated rule is deliberately broader than the renderer's: a mid-
paragraph ``<!--`` with no closer is inline raw HTML, stays visible, and is
still cut here along with everything after it.  The cost is real -- an issue
whose prose mentions those four characters outside a fenced block loses its
remainder.  It is the right trade because the failure directions are not
symmetric.  Over-stripping truncates a block a maintainer can see; under-
stripping passes text nobody saw.

*Characters that decode differently than they render.*  NFKC does not remove
these, which is the correction worth stating plainly because the instinct is
that it does: of the 170 ``Cf`` format characters, NFKC removes none.  U+200B
ZERO WIDTH SPACE, U+FEFF, and U+202E RIGHT-TO-LEFT OVERRIDE all survive it
unchanged, so a word a reviewer read as one word can still reach the model as
two, and a line can still render in an order its bytes do not have.  Format,
control, and surrogate characters are therefore dropped explicitly, before
normalising rather than after: no non-``Cf`` codepoint's NFKC output contains a
``Cf`` character, so nothing can be reintroduced, and the returned block is
itself in NFKC form for anything downstream that hashes it.

*Lookalikes.*  This is what NFKC is for, and it does it well: fullwidth forms
fold to ASCII, a non-breaking space becomes a space.

The fence
---------

The block's delimiter is derived from the digest of the text it wraps, then
checked against that text and re-derived until it does not occur there.

Deterministic, which a ``secrets.token_hex`` nonce would not be: the same title
and body produce the same block on every machine and every call, so a caller
hashing the prompt gets a stable value and replay stays byte-identical.

Unforgeable, which a fixed ``<<<ISSUE_TEXT>>>`` marker would not be: an author
who wants their text to contain the fence must find a body whose own digest
appears inside itself, and the occurrence check makes that a guarantee by
construction rather than a probability argument.

What this does not do
---------------------

It does not make a model obey the frame.  A delimiter is a boundary the text
cannot move, not obedience, and the sentence telling an agent to treat the
block as data belongs to the prompt template that composes it, not here.

It does not prove sanitized text never reaches a shell, the environment, or the
network.  That is a property of which subprocess calls the runner makes and
with what environment (#4032).  This module holds up its end by having no route
to any of them: it imports nothing but :mod:`hashlib`, :mod:`re`, and
:mod:`unicodedata`, and a test pins that list.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

__all__ = [
    "ISSUE_TEXT_FENCE_LABEL",
    "normalize_untrusted_text",
    "sanitize_issue_text",
    "strip_html_comments",
]

#: Name carried by both fence lines, so a reader (and a caller looking for the
#: boundary) can find them without knowing the per-call token.
ISSUE_TEXT_FENCE_LABEL = "UNTRUSTED-ISSUE-TEXT"

#: Hex characters taken from the digest per derivation round.  Sixteen is 64
#: bits, which is already past guessing; the loop below is what makes absence a
#: guarantee rather than a bet, so this is a readability choice, not a security
#: parameter.
_FENCE_TOKEN_CHUNK = 16

#: A closed comment, across any number of lines.  Without ``DOTALL`` a
#: multi-line comment loses its opener and keeps its content and its ``-->``,
#: which is worse than not stripping at all: the smuggled text stops looking
#: like a commented-out block and starts reading as ordinary prose.
_CLOSED_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)

#: An opener with no closer, and everything after it.  See the module docstring
#: for why this is a hiding channel rather than a typo.
_UNTERMINATED_HTML_COMMENT = re.compile(r"<!--.*\Z", re.DOTALL)

#: Unicode general categories dropped wholesale: ``Cc`` control, ``Cf`` format
#: (zero-width, bidirectional overrides, byte-order mark, soft hyphen), and
#: ``Cs`` surrogate, which reaches a string only through a lenient decoder and
#: raises on the way back out.
_INVISIBLE_CATEGORIES = frozenset({"Cc", "Cf", "Cs"})

#: The two controls that carry meaning a reader can see, kept despite ``Cc``.
_LEGIBLE_CONTROLS = frozenset({"\n", "\t"})


def strip_html_comments(text: str) -> str:
    """Return ``text`` without anything an HTML comment could be hiding.

    Closed comments are removed wherever they appear; an unterminated ``<!--``
    removes everything from itself to the end of the input.

    Args:
        text: Untrusted Markdown, exactly as the tracker's API returned it.

    Returns:
        The text a reviewer of the rendered page could actually have seen, less
        the mid-paragraph unterminated case this deliberately over-strips.
    """
    return _UNTERMINATED_HTML_COMMENT.sub("", _CLOSED_HTML_COMMENT.sub("", text))


def normalize_untrusted_text(text: str) -> str:
    """Return ``text`` with every render-versus-decode gap closed, unfenced.

    The transform :func:`sanitize_issue_text` applies before it wraps anything,
    exposed on its own because issue text is not the only place this program
    quotes a repository it does not control -- a claim comment (#3873) carries
    the same input and wants the same normalisation without a prompt fence
    around it.

    Steps, in the order they have to run: strip HTML comments (before anything
    rewrites the delimiters they are recognised by), fold line endings, drop
    invisible characters, then NFKC.  Dropping before normalising is what makes
    the result NFKC-normalised rather than merely NFKC-derived.

    Args:
        text: Untrusted text from a repository the donor does not control.

    Returns:
        Normalised text containing no control character other than newline and
        tab, no format or surrogate character, and no HTML comment.
    """
    visible = strip_html_comments(text)
    # Before dropping controls, not after: ``\r`` is a ``Cc`` character, and
    # deleting a lone one would glue the lines it separated into one word.
    unified = visible.replace("\r\n", "\n").replace("\r", "\n")
    legible = "".join(
        ch for ch in unified if ch in _LEGIBLE_CONTROLS or unicodedata.category(ch) not in _INVISIBLE_CATEGORIES
    )
    return unicodedata.normalize("NFKC", legible)


def sanitize_issue_text(title: str, body: str) -> str:
    """Return one delimited block quoting an untrusted issue title and body.

    Pure: the same arguments produce the same string on every call and every
    machine, and nothing here reads a clock, an environment, or a random
    source.

    Args:
        title: The issue title, unmodified from the tracker.
        body: The issue body, unmodified from the tracker.

    Returns:
        A block whose first and last lines are the opening and closing fence,
        each carrying :data:`ISSUE_TEXT_FENCE_LABEL` and a token derived from
        the normalised content and guaranteed absent from it.
    """
    payload = _payload(normalize_untrusted_text(title), normalize_untrusted_text(body))
    token = _fence_token(payload)
    opening = f"----- BEGIN {ISSUE_TEXT_FENCE_LABEL} {token} -----"
    closing = f"----- END {ISSUE_TEXT_FENCE_LABEL} {token} -----"
    return f"{opening}\n{payload}\n{closing}"


def _payload(title: str, body: str) -> str:
    """Lay the two normalised fields out as the block's contents.

    Both are untrusted and both are inside the same fence, so a title that
    contains a newline reads as more title rather than as the start of the
    body; there is no boundary between them for it to forge.
    """
    return f"title: {title}".rstrip() + f"\n\n{body}".rstrip()


def _fence_token(payload: str) -> str:
    """Return a token derived from ``payload`` and not occurring inside it.

    Terminates, and not merely with high probability: every round appends
    another :data:`_FENCE_TOKEN_CHUNK` characters, so after enough rounds the
    token is longer than the payload and cannot be a substring of it whatever
    the digests were.  The first round succeeds unless someone found a fixed
    point of SHA-256 inside their own issue body.
    """
    token = ""
    counter = 0
    while not token or token in payload:
        digest = hashlib.sha256(f"{counter}\x00{payload}".encode()).hexdigest()
        token += digest[:_FENCE_TOKEN_CHUNK]
        counter += 1
    return token
