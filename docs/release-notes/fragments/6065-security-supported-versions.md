## SECURITY.md's Supported Versions table now matches the shipped release

The table has fallen a release behind more than once: it said 3.19.x after
the project moved to 3.20.0, and still said 3.20.x after 3.21.0 shipped,
understating which release actually gets fixes.

The table now names 3.21.x as supported, and a regression test derives the
expected line from `pyproject.toml`'s version at test time, so a future
release bump that forgets to touch `SECURITY.md` fails CI instead of
shipping a stale policy (#6065).
