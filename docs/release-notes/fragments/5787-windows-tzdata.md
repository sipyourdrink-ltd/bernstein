## Schedules load on Windows

`schedule_kinds` instantiates `ZoneInfo("UTC")` at import time and resolves
configured zone names dynamically, and `zoneinfo` has no tz database to consult
on Windows or on containers without one, so the import raised
`ZoneInfoNotFoundError: 'No time zone found with key UTC'`. `tzdata` is now a
direct dependency: a no-op on POSIX (the system database is consulted first and
takes precedence), and the IANA database everywhere else. Unlocks 8 previously
uncollectable unit test files on the Windows lane (#5787).