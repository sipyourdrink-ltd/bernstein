"""The Luhn checksum, in one place (issue #3820).

Four call sites decide whether a digit run is a payment card, and three of
them ran their own copy of this checksum. The copies agreed on the arithmetic
but were spelled differently -- one walked the string reversed and doubled the
odd indices, another walked forward and doubled ``index % 2 == len % 2`` --
which are the same rule written two ways, so a reader comparing them had to
derive Luhn from scratch to confirm it.

What deliberately does **not** live here is the **length policy**. The bound
legitimately differs per call site: a fixed 16-digit regex needs none, a
labelled 13-19 digit match needs both ends. Callers keep their own bound and
pass a stripped digit run in.

Kept dependency-free (no imports at all) so the logging path can use it
without pulling ``core/security`` machinery into every log record.
"""

from __future__ import annotations

__all__ = ["luhn_check"]


def luhn_check(digits: str) -> bool:
    """Return True when *digits* satisfies the Luhn checksum.

    Args:
        digits: A run of decimal digits with separators already stripped.
            Anything else -- empty, non-decimal, signed -- returns ``False``
            rather than raising.

    Returns:
        ``True`` when the run checksums as a card number.

    Note:
        This is a checksum, not a card test. ``"0" * 16`` passes. Callers
        must apply their own length policy, and a checksum-valid run of the
        right length is still only card-*shaped*.
    """
    # ``str.isdecimal()``, not ``str.isdigit()``: isdigit() also accepts
    # characters like superscript two, which int() then refuses with a
    # ValueError. Both are true of exactly the same strings the callers can
    # produce (they strip to ``\d``, which is decimals), so this costs
    # nothing and removes a latent crash for any future caller.
    if not digits or not digits.isdecimal():
        return False
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0
