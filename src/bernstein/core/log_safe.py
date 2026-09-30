"""Bound and de-fang an arbitrary value before it becomes part of a log record.

A log file is newline-delimited for every reader that consumes it afterwards -
a human scrolling the file, ``journalctl``, a log shipper splitting a stream
into records. A request field, webhook payload, or database row that started
as one of those can carry ``\\r`` or ``\\n`` verbatim. Interpolate it into a log
message unexamined and the value has written a second, fabricated log line
that reads as if the application emitted it: a forged audit entry, a spoofed
error a human acts on, or a record a downstream parser was never meant to see.

:func:`bernstein.core.security.sanitize.sanitize_log` already owns the
character-class defense - the record-boundary and control-character set is
declared once there, and every module in this codebase that logs untrusted
text calls it. :func:`for_log` builds on it rather than beside it, and adds
the two things call sites otherwise reimplement ad hoc, inconsistently, or
skip:

* **Any object, not just ``str``.** A caller reaching for a log helper at the
  point of a ``logger.*()`` call usually has an id, an enum member, a
  ``Path``, or an exception in hand, not text. Requiring ``str(...)`` first is
  exactly the step that gets forgotten under review.
* **A bound on the result.** ``sanitize_log`` escapes what is unsafe about a
  value; it does not limit how much of it reaches the log. A value an
  attacker fully controls could otherwise grow a log file in proportion to
  however much they choose to send in one field.

The one guarantee on top of both: it never raises. A value reaching a log
call is exactly the value least worth trusting, and a hostile ``__str__``
must not take the log call - or the request that produced it - down with it.

Call this rather than writing a local escaper. CodeQL cannot infer that a
function sanitizes; every barrier is declared by name in
``.github/codeql/models/bernstein-sanitizers.model.yml``, so a module-local
copy raises ``py/log-injection`` on each of its call sites until somebody
adds a row for it. Those alerts are indistinguishable from real findings,
which is what makes the duplication expensive rather than merely untidy.
Pass ``limit=`` when a call site needs a tighter bound than the default.
"""

from __future__ import annotations

from bernstein.core.security.sanitize import sanitize_log

__all__ = ["for_log"]

#: Marker appended when a value is cut short, matching the truncation
#: convention already used for bounded log/diff text elsewhere in this
#: codebase.
_TRUNCATION_MARKER = "...(truncated)"


def for_log(value: object, *, limit: int = 512) -> str:
    """Return *value* as a bounded, record-boundary-safe token for a log call.

    Wrap any argument sourced from outside the process - a request field, a
    webhook payload, a header, a database row that started as one of those -
    before it reaches a ``logger.*()`` call.

    Args:
        value: The value to log. Converted with ``str()``; need not already
            be a string.
        limit: Maximum length of the returned text, excluding the truncation
            marker. Defaults to 512, which is generous for an id or a reason
            string and still bounded for a value an attacker controls.

    Returns:
        *value* as text, with every character
        :func:`~bernstein.core.security.sanitize.sanitize_log` treats as
        unsafe (CR, LF, every other C0/C1 control character, and the Unicode
        line/paragraph separators) replaced by a printable escape sequence,
        cut to *limit* characters with ``"...(truncated)"`` appended when it
        was, or ``"<unprintable {type name}>"`` when *value* cannot be turned
        into text at all. Always a plain ``str`` - this function does not
        raise.
    """
    try:
        text = sanitize_log(str(value))
    except Exception:
        return f"<unprintable {type(value).__name__}>"
    if len(text) > limit:
        text = text[:limit] + _TRUNCATION_MARKER
    return text
