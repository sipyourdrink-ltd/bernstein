"""Evergreen guards that keep the agent-context mirror files current.

These are the anti-rot half of the ``bernstein agents-md`` system. The
generator + bridge already render the mirrors; ``bernstein agents-md verify``
gates them in CI. These tests add three protections that run under a plain
``pytest`` invocation (no bespoke workflow), so drift is caught locally and on
every shard, not only in the dedicated drift job:

1. :class:`TestMirrorsInSync` -- regenerate every mirror file in-memory from
   the canonical generator and bridge and assert the committed bytes match. The
   unit-test twin of ``bernstein agents-md verify``. Covers *all* mirror files:
   ``AGENTS.md``, ``CLAUDE.md``, ``CONVENTIONS.md``, ``.aider.conf.yml``,
   ``.goosehints`` and every ``.cursor/rules/*.mdc``. Also asserts no orphan
   ``.mdc`` lingers after a section is removed.
2. :class:`TestReferencedPathsExist` -- every repo-relative path referenced in
   the canonical ``AGENTS.md`` under a checkable root must exist on disk, so a
   rename or delete that leaves a doc dangling turns red.
3. :class:`TestSecurityTxtFreshness` -- the ``.well-known/security.txt``
   ``Expires`` field must parse and sit more than 30 days in the future, so the
   renewal PR lands before RFC 9116 consumers begin rejecting the file.
"""

from __future__ import annotations

import re
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bernstein.core.knowledge.agents_md_bridge import ALL_TARGETS, render
from bernstein.core.knowledge.agents_md_generator import generate

# ---------------------------------------------------------------------------
# Repo location helpers
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]


def _repo_name(repo_root: Path) -> str:
    """Mirror the CLI's ``[project] name`` inference so the H1 matches on disk."""
    pyproject = repo_root / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    project = data.get("project", {})
    name = project.get("name") if isinstance(project, dict) else None
    return name if isinstance(name, str) and name.strip() else repo_root.name


def _is_bernstein_checkout(repo_root: Path) -> bool:
    return (repo_root / "AGENTS.md").is_file() and (repo_root / "src" / "bernstein").is_dir()


pytestmark = pytest.mark.skipif(
    not _is_bernstein_checkout(REPO_ROOT),
    reason="mirror guards only run inside a bernstein source checkout",
)


# ---------------------------------------------------------------------------
# 1. Mirrors in sync with the generator
# ---------------------------------------------------------------------------


class TestMirrorsInSync:
    """The committed mirrors must equal a fresh in-memory regeneration.

    Equivalent to ``bernstein agents-md verify``: any hand-edit to a mirror,
    or any code change that shifts a derived section (module docstring, script
    entry, role template, build tooling) without a follow-up
    ``bernstein agents-md sync``, fails here.
    """

    def _sections_and_name(self) -> tuple[list, str]:
        sections = generate(REPO_ROOT)
        assert sections, "generator produced no sections for the repo root"
        return sections, _repo_name(REPO_ROOT)

    def test_every_generated_file_matches_disk(self) -> None:
        sections, name = self._sections_and_name()
        checked = 0
        for target in ALL_TARGETS:
            output = render(sections, target, repo_name=name)
            for rel, expected in output.files.items():
                on_disk = REPO_ROOT / rel
                assert on_disk.is_file(), f"missing mirror file {rel} (target={target}); run `bernstein agents-md sync`"
                actual = on_disk.read_text(encoding="utf-8")
                # Normalise trailing whitespace / final newline exactly as the
                # verify command does; anything else is genuine drift.
                assert actual.rstrip() == expected.rstrip(), (
                    f"{rel} drifted from the generator (target={target}); run `bernstein agents-md sync`"
                )
                checked += 1
        assert checked >= 13, f"expected to check at least 13 mirror files, checked {checked}"

    def test_no_orphan_cursor_rules(self) -> None:
        """A removed section must not leave a stale ``.cursor/rules/*.mdc`` behind."""
        sections, name = self._sections_and_name()
        generated = set(render(sections, "cursor", repo_name=name).files)
        rules_dir = REPO_ROOT / ".cursor" / "rules"
        on_disk = {f".cursor/rules/{p.name}" for p in rules_dir.glob("*.mdc")}
        orphans = on_disk - generated
        assert not orphans, f"orphan cursor rule file(s) not produced by the generator: {sorted(orphans)}"


# ---------------------------------------------------------------------------
# 2. Referenced repo paths exist
# ---------------------------------------------------------------------------


# Single-backtick-delimited tokens (not part of a longer backtick run, so RST
# ``double-backtick`` module spans are ignored) that contain a "/".
_PATH_TOKEN = re.compile(r"(?<!`)`([A-Za-z0-9_.][A-Za-z0-9_./-]*/[A-Za-z0-9_./-]*)`(?!`)")

# Only paths under these roots are unambiguously repo-relative. Module-map
# sub-package rows (e.g. ``admission/``) are package-relative and intentionally
# excluded, as are ``.sdd/`` runtime dirs that do not exist in a clean checkout.
_CHECKABLE_ROOTS = ("src/", "tests/", "templates/", "scripts/", "docs/")

# Intentional examples / placeholders referenced in prose that are not expected
# to exist as literal paths. Keep this list tight and documented.
_PATH_ALLOWLIST: frozenset[str] = frozenset()


class TestReferencedPathsExist:
    def test_backtick_repo_paths_resolve(self) -> None:
        text = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
        tokens = sorted(set(_PATH_TOKEN.findall(text)))
        checkable = [
            t
            for t in tokens
            if t.startswith(_CHECKABLE_ROOTS) and t not in _PATH_ALLOWLIST and not any(c in t for c in "<>*")
        ]
        assert checkable, "expected at least one checkable repo path in AGENTS.md"
        missing = [t for t in checkable if not (REPO_ROOT / t).exists()]
        assert not missing, f"AGENTS.md references repo paths that do not exist: {missing}"


# ---------------------------------------------------------------------------
# 3. security.txt freshness (RFC 9116)
# ---------------------------------------------------------------------------


_MIN_DAYS_BEFORE_EXPIRY = 30


class TestSecurityTxtFreshness:
    def test_expires_parses_and_is_more_than_30_days_out(self) -> None:
        path = REPO_ROOT / ".well-known" / "security.txt"
        assert path.is_file(), "missing .well-known/security.txt"
        lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()]
        expires_lines = [ln for ln in lines if ln.lower().startswith("expires:")]
        assert len(expires_lines) == 1, f"expected exactly one Expires field, found {len(expires_lines)}"
        raw = expires_lines[0].split(":", 1)[1].strip()
        # RFC 9116 requires an ISO 8601 timestamp; normalise a trailing Z so
        # fromisoformat accepts it across Python versions.
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        assert parsed.tzinfo is not None, f"Expires must be timezone-aware: {raw!r}"
        days_left = (parsed - datetime.now(UTC)).days
        assert days_left > _MIN_DAYS_BEFORE_EXPIRY, (
            f"security.txt Expires is only {days_left} day(s) out ({raw}); "
            f"bump it so renewal lands before the {_MIN_DAYS_BEFORE_EXPIRY}-day window closes"
        )
