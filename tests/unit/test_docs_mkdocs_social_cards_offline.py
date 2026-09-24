"""Guard against re-introducing the mkdocs-material `social` plugin's live
Google Fonts fetch, which took bernstein.readthedocs.io's build down: Read
the Docs starts each build from a fresh container, so the plugin's on-disk
font cache never warms up and every build re-hits fonts.google.com, an
endpoint that started rejecting shared RTD/CI build IPs.

``mkdocs build`` itself needs the ``docs`` dependency group (not installed
in the unit-test env, and `tests/AGENTS.md` bars network from `unit/`
anyway), so this only checks the plugin config that controls whether card
image generation - the codepath that fetches fonts - runs at all. The full
build is exercised by the `docs` CI job with `mkdocs build --strict`.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _social_plugin_block() -> str:
    mkdocs_yml = (_REPO_ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    match = re.search(r"^  - social:\n((?:^ {6}.*\n|^\n)*)", mkdocs_yml, re.MULTILINE)
    assert match, "mkdocs.yml has no top-level `social` plugin entry"
    return match.group(1)


def test_social_card_image_generation_is_disabled() -> None:
    block = _social_plugin_block()
    assert re.search(r"^\s*cards:\s*false\s*$", block, re.MULTILINE), (
        "social plugin's `cards` option must stay `false`: with cards "
        "enabled, mkdocs build fetches the theme font from "
        "fonts.google.com on every RTD build (fresh container, no cache) "
        "which is what broke bernstein.readthedocs.io - see the comment "
        "above `- social:` in mkdocs.yml"
    )
