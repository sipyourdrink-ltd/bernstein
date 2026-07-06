"""Guard: shipped source points at the canonical docs host only.

The documentation site moved to ``bernstein.readthedocs.io``. The old
GitHub Pages host (``chernistry.github.io/bernstein``) no longer serves
docs, so any user-facing link that still points there is a dead link in
the wheel. This test scans the shipped source tree so a regression that
reintroduces the stale host fails CI, the same way the lineage-spine
deprecation guard pins v1 writer construction sites.
"""

from __future__ import annotations

from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src" / "bernstein"

_DEAD_DOCS_HOST = "chernistry.github.io"


def _dead_link_sites() -> list[str]:
    hits: list[str] = []
    for py in _SRC.rglob("*.py"):
        try:
            text = py.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _DEAD_DOCS_HOST in line:
                hits.append(f"{py}:{lineno}")
    return hits


def test_no_dead_docs_host_in_src() -> None:
    sites = _dead_link_sites()
    assert sites == [], (
        f"shipped source must not link to the retired docs host "
        f"'{_DEAD_DOCS_HOST}'; use https://bernstein.readthedocs.io/ instead:\n"
        + "\n".join(sites)
    )
