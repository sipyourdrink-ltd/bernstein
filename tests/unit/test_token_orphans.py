"""No module under ``core/tokens/`` may sit in the tree with no caller.

A module with a green unit suite and no runtime caller reads as a working
feature: a contributor extends it, a reviewer trusts it, and CI pays for a
suite that protects nothing. This test makes the next such module fail CI
instead of joining the pile.

The scan itself lives in ``tests/unit/_orphan_scan.py`` and is shared with
``test_orphan_security_modules.py``; only the scanned subpackage and the
allowlist below are local to this file.
"""

from __future__ import annotations

from tests.unit._orphan_scan import REPO_ROOT, SubpackageScan, assert_orphans_match

TOKENS_DIR = REPO_ROOT / "src" / "bernstein" / "core" / "tokens"
SCAN = SubpackageScan(directory=TOKENS_DIR, package="bernstein.core.tokens")

# Modules known to have no caller today, kept as an exact set rather than a
# floor: a new orphan fails this test, and removing one of these fails it too
# until the name is struck from the list. The list only ever shrinks.
KNOWN_ORPHANS = frozenset(
    {
        "cache_token_tracker",
        "claude_prompt_cache_optimizer",
        "context_fallback",
        "context_inheritance",
        "image_optimizer",
        "prompt_injection",
        "token_binding",
        "token_breakdown",
        "token_counter",
    }
)


def test_no_new_orphan_token_modules() -> None:
    """The set of caller-less modules may shrink, never grow."""
    assert_orphans_match(SCAN, KNOWN_ORPHANS, "core/tokens/")


def test_a_wired_module_is_seen_through_its_legacy_alias() -> None:
    """`token_monitor` is reachable only via `bernstein.core.token_monitor`.

    Without redirect resolution the scan calls it an orphan, which is how a
    detector ends up demanding the deletion of a module the orchestrator
    imports on every run.
    """
    assert "token_monitor" in SCAN.module_names()
    assert SCAN.importer_of("token_monitor") is not None


def test_the_alias_table_alone_does_not_count_as_a_caller() -> None:
    """Otherwise every module listed in the redirect map reads as reachable."""
    assert KNOWN_ORPHANS, "the guard needs at least one known orphan to be meaningful"
    orphan = sorted(KNOWN_ORPHANS)[0]
    alias_table = (REPO_ROOT / "src" / "bernstein" / "core" / "__init__.py").read_text(encoding="utf-8")
    assert f'"{orphan}"' in alias_table, f"{orphan} is expected to be listed in the alias table"
    assert SCAN.importer_of(orphan) is None


def test_a_mutually_importing_dead_cluster_is_still_orphaned() -> None:
    """Two dead modules that import each other must not vouch for each other.

    This is the failure the "does anything import it?" shape cannot see, and
    it is how a whole dead corner of a package survives a cleanup.
    """
    live_caller = REPO_ROOT / "src" / "bernstein" / "core" / "orchestration" / "orchestrator.py"
    importers = {
        "wired": {live_caller},
        "dead_a": {TOKENS_DIR / "dead_b.py"},
        "dead_b": {TOKENS_DIR / "dead_a.py"},
    }
    assert SCAN.reachable_modules(importers) == {"wired"}


def test_a_module_used_only_by_a_wired_module_is_reachable() -> None:
    """Positive control: intra-package edges do carry reachability."""
    live_caller = REPO_ROOT / "src" / "bernstein" / "core" / "orchestration" / "orchestrator.py"
    importers = {
        "wired": {live_caller},
        "helper": {TOKENS_DIR / "wired.py"},
    }
    assert SCAN.reachable_modules(importers) == {"wired", "helper"}
