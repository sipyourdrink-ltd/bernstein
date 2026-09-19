## Conformance vectors for the authority plane

Four auditor questions now have vectors — 3 (was the sub-agent authorized, and
by whom), 4 (what exactly was it permitted to do), 5 (did it stay inside that
permission) and 21 (which other principals hold authority derived from the same
grant).

All four fail, as `xfail(strict=True)`, and that is the deliverable. The bundle
carries no grant, capability ceiling, permitted scope or delegation receipt of
any kind, so the permitted set is unreadable even in principle and containment
has nothing to be compared against. Each vector names the field that is missing
and the issue that would add it, and `strict` means the day one lands the build
fails until the vector is un-marked.

The bundle is searched structurally — whole object keys and whole event types —
rather than by substring, because the vocabulary of authority overlaps the
vocabulary of everything else. A run already writes a journal event whose
payload key is literally `capability`, meaning mutation-observability and not
permission, and any audit detail quoting `401 Unauthorized` contains the string
`authorized`. Under a substring search either one would have reported that the
bundle answers question 3, with `strict=True` turning that false green into a
failing build. The search also reads inside `article12.zip`, so a record
landing in the archive is found rather than missed.

Question 5 asks its own question rather than repeating question 4's: the
permitted set existing is question 4, and question 5 compares every tool
agent-B called and every path it touched against that set, so it can only go
green if agent-B actually stayed inside its grant.

The auditor scoreboard now asks 10 of 21 questions, up from 6 (#5059).
