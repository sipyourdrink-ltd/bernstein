"""Which repository-relative paths a unified diff touches.

Two boundaries in this repository decide whether a patch may be accepted by
asking that one question, and both fail *open* when the answer misses a path:
the Tier-3 auto-heal cordon (:mod:`bernstein.core.autofix.tier3`) and the
volunteer program's ``allowed_paths`` enforcement
(:mod:`bernstein.core.volunteer.task_finish`).  A path the extractor does not
report is a file the check never inspects, so the write happens unchallenged.

So the answer lives here once, for the same reason
:mod:`bernstein.core.path_scope` exists: a patch judged in scope by one caller
and out of scope by the other, for the same diff, is a bug in whichever one you
were not looking at.

The invariant is asymmetric on purpose
--------------------------------------

**Over-approximate; never under-approximate.**  A path reported here that the
diff does not really touch costs a refusal the contributor can read and argue
with.  A path *not* reported costs an unreviewed write outside whatever
boundary the caller was enforcing.  Every construct that could name a file
therefore contributes, an ambiguous header contributes every candidate split,
and a header that cannot be parsed contributes its raw tokens rather than
nothing.

The same asymmetry answers the obvious objection to reading headers out of a
diff at all: a ``+`` content line can forge one.  A forged header adds a path
to the result, which is the harmless direction -- at worst a legitimate patch
is refused, loudly.  A parser that tried to be clever about which headers are
"real" would be trading that for the silent direction.

Where the paths hide
--------------------

``---``/``+++`` header pairs are the obvious source, and were the only one the
first version of this code read.  They are absent from three shapes ``git
diff`` emits routinely, and each one is a way to touch a file with no hunk at
all:

* a rename or copy that changes no content -- ``rename from``/``rename to``,
  ``copy from``/``copy to``, and no ``@@`` hunk anywhere;
* a mode change -- ``old mode``/``new mode``, which is how a file becomes
  executable;
* a binary file -- ``Binary files a/x and b/x differ``, or ``GIT binary
  patch``.

In all three the ``diff --git`` header is the only place the paths appear, so
it is read as well.

Quoting
-------

Git C-quotes a path containing a quote, a backslash, a control character, or a
non-ASCII byte: ``diff --git "a/caf\303\251.py" "b/caf\303\251.py"``.  Those
are unquoted here so an ordinary non-ASCII filename produces the path a caller
can match against its globs, rather than a mangled string that every scope
check refuses.  A token that will not unquote contributes verbatim -- the
refusing direction again.

The one shape this cannot disambiguate
--------------------------------------

``a/`` and ``b/`` prefixes are dropped, because that is what ``git diff``
prints.  A diff produced with ``--no-prefix`` from a repository that has a
top-level directory actually named ``a`` or ``b`` is therefore read one
segment short.  There is no signal in the diff that distinguishes the two, so
the resolution is upstream: whoever produces a diff for a security check
leaves the prefixes on.
"""

from __future__ import annotations

__all__ = ["extract_paths_from_unified_diff"]

#: Header prefixes that announce a file pair, mapped to how many sides they
#: carry.  ``rename``/``copy`` lines are one path each and appear in pairs.
_DIFF_GIT_PREFIX = "diff --git "
_OLD_SIDE_PREFIX = "--- "
_NEW_SIDE_PREFIX = "+++ "
_SINGLE_PATH_PREFIXES = ("rename from ", "rename to ", "copy from ", "copy to ")

#: The new-side path of a deletion and the old-side path of an addition.  Not
#: a repository path, and passing it to a scope check would refuse every added
#: or deleted file.
_DEV_NULL = "/dev/null"

#: The ``b``-side marker inside a ``diff --git`` header's tail.  Git does not
#: quote a path merely for containing a space, so the split is ambiguous when
#: a path contains ``" b/"`` itself; see :func:`_header_pair_paths`.
_B_SIDE_MARKER = " b/"

_C_ESCAPES = {
    "a": "\a",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
    "\\": "\\",
    '"': '"',
}


def extract_paths_from_unified_diff(diff: str) -> tuple[str, ...]:
    """Return every repository path the diff could touch, in first-seen order.

    Deduplicated, so a file announced by its ``diff --git`` header and again by
    its ``+++`` header appears once.  ``/dev/null`` is never returned.

    The result is an over-approximation by design (see the module docstring):
    callers may refuse a path that turns out not to be touched, and must never
    assume a path absent from this result is untouched.

    Args:
        diff: A unified diff, as ``git diff`` prints it.  Text this process
            did not author, and possibly not text a git it trusts authored.

    Returns:
        The paths, repository-relative and in the diff's own order (old side
        before new side within a pair).  Empty for an empty diff -- and, for a
        non-empty diff, empty only when it names nothing this module
        recognises, which a security-sensitive caller should treat as a
        failure to read the patch rather than as "no files touched".
    """
    collected: list[str] = []
    seen: set[str] = set()

    def add(path: str) -> None:
        if not path or path == _DEV_NULL or path in seen:
            return
        seen.add(path)
        collected.append(path)

    for line in diff.splitlines():
        if line.startswith(_DIFF_GIT_PREFIX):
            for path in _header_pair_paths(line[len(_DIFF_GIT_PREFIX) :]):
                add(path)
            continue
        if line.startswith(_OLD_SIDE_PREFIX):
            add(_side_path(line[len(_OLD_SIDE_PREFIX) :]))
            continue
        if line.startswith(_NEW_SIDE_PREFIX):
            add(_side_path(line[len(_NEW_SIDE_PREFIX) :]))
            continue
        for prefix in _SINGLE_PATH_PREFIXES:
            if line.startswith(prefix):
                # ``rename from``/``rename to`` carry the path with no ``a/``
                # or ``b/`` prefix, and are the only announcement a
                # content-preserving rename makes.
                add(_unquote(line[len(prefix) :].strip()))
                break

    return tuple(collected)


def _side_path(rest: str) -> str:
    """Read one ``---``/``+++`` tail: unquote first, then drop git's prefix.

    Order matters.  Git quotes the *whole* token including the prefix
    (``--- "a/caf\\303\\251.md"``), so a caller that stripped first would be
    looking for ``a/`` behind a quote character, keep the prefix, and hand
    every scope check a path one segment too deep.
    """
    return _drop_side_prefix(_unquote(rest.strip()))


def _drop_side_prefix(path: str) -> str:
    """Drop the single ``a/`` or ``b/`` git puts in front of a diff path."""
    for prefix in ("a/", "b/"):
        if path.startswith(prefix):
            return path[len(prefix) :]
    return path


def _header_pair_paths(rest: str) -> tuple[str, ...]:
    """Split a ``diff --git`` header's tail into the paths it could name.

    Git writes ``a/<old> b/<new>`` and does not quote either side merely for
    containing a space, which makes the split genuinely ambiguous for a path
    like ``x b/y.sh``.  Git's own tooling sidesteps this by preferring the
    ``---``/``+++`` headers, and this module reads those too; the header is
    load-bearing only for the pairs that print no hunk at all -- renames, mode
    changes, binary files.

    Ambiguity is resolved by contributing *every* candidate split rather than
    picking one.  The true pair is always among them, and a spurious extra
    path can only produce a refusal.

    A quoted tail takes the other route: a C-quoted token contains no
    unescaped whitespace, so splitting on whitespace there is exact rather
    than a guess.  So is a tail with no ``" b/"`` in it at all, which is what
    ``--no-prefix`` produces.
    """
    candidates: list[str] = []
    if '"' not in rest:
        start = 0
        while (index := rest.find(_B_SIDE_MARKER, start)) != -1:
            candidates.append(_drop_side_prefix(rest[:index]))
            # The marker consumed the ``b/``; what follows it is the path.
            candidates.append(rest[index + len(_B_SIDE_MARKER) :])
            start = index + 1

    if not candidates:
        candidates = [_drop_side_prefix(_unquote(token)) for token in rest.split()]
    return tuple(candidate for candidate in candidates if candidate)


def _unquote(token: str) -> str:
    """Decode git's C-style quoting, or return the token untouched.

    Only a token wrapped in double quotes is treated as quoted -- that is
    git's own rule, so this cannot start decoding an ordinary filename that
    merely contains a quote character.  Octal escapes are decoded to bytes and
    then to text, because a multi-byte character is emitted one octal escape
    per byte and decoding them individually would produce mojibake instead of
    the filename.

    A token that does not decode is returned as it arrived: a caller's scope
    check then refuses it, which is the direction a parse failure should take.
    """
    if len(token) < 2 or not token.startswith('"') or not token.endswith('"'):
        return token
    body = token[1:-1]
    out = bytearray()
    index = 0
    while index < len(body):
        char = body[index]
        if char != "\\":
            out.extend(char.encode("utf-8"))
            index += 1
            continue
        if index + 1 >= len(body):
            return token
        nxt = body[index + 1]
        if nxt in _C_ESCAPES:
            out.extend(_C_ESCAPES[nxt].encode("utf-8"))
            index += 2
            continue
        octal = body[index + 1 : index + 4]
        if len(octal) == 3 and all(digit in "01234567" for digit in octal):
            out.append(int(octal, 8))
            index += 4
            continue
        return token
    return out.decode("utf-8", errors="surrogateescape")
