## SECURITY.md's Supported Versions table now matches the shipped release

The table still said 3.19.x was the supported line after the project had
moved on to 3.20.0, understating which release actually gets fixes.

The table now names 3.20.x as supported, and a regression test derives the
expected line from `pyproject.toml`'s version at test time, so a future
release bump that forgets to touch `SECURITY.md` fails CI instead of
shipping a stale policy (#6065).
