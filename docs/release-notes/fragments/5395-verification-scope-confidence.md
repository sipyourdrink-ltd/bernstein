## A gate's scope says how far its claim reaches, and serialises the way receipts do

`VerificationScope` recorded which paths a gate exercised and which it could
not, but not how far that claim reached or where the evidence behind it
lives, and it had no serialisation at all — so a scope could not be folded
into a hashed subject without someone inventing a second canonical form for
it.

It now carries `confidence`, drawn from the closed set `high` / `partial` /
`none`, and `evidence_ref`. Three levels is the smallest set that lets a
later check answer "is a required oracle kind actually absent?" honestly: a
kind covered only by `none` scopes has no coverage, and collapsing `partial`
into either neighbour would force a gate to overstate or understate what it
did. Construction rejects a confidence outside the set, naming the value and
the allowed members.

Construction also rejects a bare `str` for `checked` or `cannot_check`.
`str` is iterable, so `checked="a.py"` would have silently become five
single-character "paths" and the scope would have claimed coverage of files
named `a` and `.` — a wrong claim that looked like a well-formed one.

`canonical_bytes()` uses sorted keys and compact separators, the convention
`merge_receipt._canonical_bytes` already keeps, so equal scopes hash
identically regardless of the order their fields were built in.
