# Unreleased

Changes merged to `main` that are not yet part of a tagged release. Each
tagged release has its own page in this directory; this page carries what has
landed since the newest one.

Cutting a version empties this page: every entry the tag ships moves onto that
version's page in the release PR itself. `tests/unit/test_unreleased_notes_rotation.py`
holds the page to that — an entry naming an issue or PR a tagged release page
already documents fails the build. An entry that cites released work as context
rather than as its own attribution is exempted by hand there, with the reason.

## Added

- `bernstein run` on a TTY now says it is waiting for the first agent instead of sitting silent (#4257). The wait is bounded and returns as soon as an agent registers, so a fast start stays exactly as quiet as before: the transient status appears only once a poll has already failed to produce a verdict, and clears before the dashboard or the Rich fallback renders. The non-interactive detach path is unchanged.

## Fixed

- The `bernstein` MCP bridge injected into every agent spawn pointed at `python -m bernstein.mcp.server`, a module with no `__main__` guard: running it imported the module and exited 0 without ever answering the MCP handshake (#4313). Every spawned agent's init event showed the `bernstein` server as disconnected and never received the bridge tools (`bernstein_post_message`, `bernstein_claim`, `bernstein_complete`, and the rest), so anything downstream that depends on agent-posted progress signals starved silently. The spawn spec now points at the package's own stdio entrypoint, `python -m bernstein.mcp` (`bernstein/mcp/__main__.py` -> `run_stdio()`), and `bernstein/mcp/server.py` gained a `__main__` guard calling the same `run_stdio()` directly, so the previously-specced module path answers too.
