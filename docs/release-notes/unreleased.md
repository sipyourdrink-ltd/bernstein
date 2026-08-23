# Unreleased

Changes merged to `main` that are not yet part of a tagged release. Each
tagged release has its own page in this directory; this page carries what has
landed since the newest one.

Cutting a version empties this page: every entry the tag ships moves onto that
version's page in the release PR itself. `tests/unit/test_unreleased_notes_rotation.py`
holds the page to that — an entry naming an issue or PR a tagged release page
already documents fails the build. An entry that cites released work as context
rather than as its own attribution is exempted by hand there, with the reason.

Nothing yet: v3.17.1 was cut from this page.

## Eval gate receipts read their own history

A gate verdict receipt sealed before three-valued verdicts carried no `reason`
in its evidence. The parser now defaults it, so a binary-era receipt still
parses instead of being rejected as malformed (#4182).

## `/status` agrees with itself about live agents

`summary.agents`, `agents.count` and the rendered `Active agents: N` line each
filtered the same session with a different expression, so a reaped agent could
read as active on one surface and not on another. A single `_agent_is_alive`
predicate now owns the call on every surface (#4360).
