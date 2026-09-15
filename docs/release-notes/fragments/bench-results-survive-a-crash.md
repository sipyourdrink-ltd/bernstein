## A crashed benchmark run resumes from the results it already wrote

`ResultStore` is the resume point: the harness calls `already_evaluated` per
instance so a restarted run skips work it has finished. Appending is not
atomic, so a process killed mid-write leaves a partial last line — and a bare
`json.loads` over every line made that one fragment poison the whole file.
`already_evaluated` raised, so the run could not resume, and every instance it
*had* completed was evaluated again. On SWE-Bench that is a real model call and
real money, paid twice for work already done.

A torn **final** line is now dropped with a warning naming how many complete
results survived it. A malformed line anywhere else still raises: only the last
line can be torn by an interrupted append, so forgiving the others would trade
one failure for silent data loss in the middle of a file.

`append` also flushes and `fsync`s. A finished instance sitting in the page
cache when the process dies is the same cost in the other direction.
