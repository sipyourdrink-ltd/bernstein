"""No compliance-framework check module may sit in the tree with no caller.

Seven modules across the codebase implement or report on compliance-framework
checks (SOC 2, ISO 27001, PCI DSS, NIST 800-53, HIPAA, GDPR, EU AI Act). Three
of them are wired into ``bernstein compliance`` via ``cli/commands/compliance_cmd.py``.
The other four sit in the tree with a green unit suite and zero non-test
importers: an operator running ``bernstein compliance check`` never executes
their checks, but the code looks like coverage.

This test makes the next such module fail CI instead of joining the pile.

Reachability has to be computed the way the package actually resolves
imports. A module reachable only through its ``bernstein.core.__init__``
redirect alias (see ``_REDIRECT_MAP``) is still reachable - the alias table
itself is excluded from the scan, since naming a module there is what makes
the legacy import path work, not evidence that anyone calls it. Modeled on
the same pattern ``tests/unit/test_token_orphans.py`` uses for
``core/tokens/``; read that file first if this one needs to be extended.

Scope: the seven modules named in issue #5098's "Where the code is" table.
Other files whose name happens to contain "compliance" - the CLI group
itself (``compliance_cmd.py``), the compliance *config* dataclass
(``core/security/compliance.py``, wired through ``core/compliance/__init__.py``),
the SOC 2 storage exporter (``core/storage/soc2_export.py``, wired through
``core/storage/__init__.py``) - are entry points or unrelated concerns, not
framework-check modules, and are already reachable; they are intentionally
left out of ``CANDIDATES`` rather than padding it with modules that were
never at risk.
"""

from __future__ import annotations

import ast
from functools import lru_cache
from pathlib import Path

from bernstein.core import _REDIRECT_MAP

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src" / "bernstein"

# label -> (file, canonical dotted import path)
CANDIDATES: dict[str, tuple[Path, str]] = {
    "compliance_library": (
        SRC / "core" / "security" / "compliance_library.py",
        "bernstein.core.security.compliance_library",
    ),
    "hipaa": (
        SRC / "core" / "security" / "hipaa.py",
        "bernstein.core.security.hipaa",
    ),
    "soc2_report": (
        SRC / "core" / "security" / "soc2_report.py",
        "bernstein.core.security.soc2_report",
    ),
    "compliance_report": (
        SRC / "core" / "security" / "compliance_report.py",
        "bernstein.core.security.compliance_report",
    ),
    "compliance_policies": (
        SRC / "core" / "security" / "compliance_policies.py",
        "bernstein.core.security.compliance_policies",
    ),
    "compliance_pack": (
        SRC / "core" / "compliance" / "pack.py",
        "bernstein.core.compliance.pack",
    ),
    "eu_ai_act": (
        SRC / "compliance" / "eu_ai_act.py",
        "bernstein.compliance.eu_ai_act",
    ),
}

# The alias table is a redirect declaration, not a call site.
EXCLUDED_FROM_SCAN = {SRC / "core" / "__init__.py"}

# Modules known to have no caller today, kept as an exact set rather than a
# floor: a new orphan fails this test, and removing one of these fails it
# too until the name is struck from the list. The list only ever shrinks -
# each slice of #5098 that wires or deletes one of these four strikes it.
KNOWN_ORPHANS = frozenset({"compliance_library", "hipaa", "soc2_report", "compliance_report"})


def _scanned_files() -> list[Path]:
    return [p for p in SRC.rglob("*.py") if p not in EXCLUDED_FROM_SCAN]


@lru_cache(maxsize=1)
def _import_index() -> tuple[dict[str, list[Path]], dict[tuple[str, str], list[Path]]]:
    """One AST pass over ``src`` instead of one per candidate module."""
    dotted: dict[str, list[Path]] = {}
    from_package: dict[tuple[str, str], list[Path]] = {}

    for path in _scanned_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                source = node.module or ""
                dotted.setdefault(source, []).append(path)
                for alias in node.names:
                    from_package.setdefault((source, alias.name), []).append(path)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    dotted.setdefault(alias.name, []).append(path)

    return dotted, from_package


def _import_targets(label: str) -> set[str]:
    """Every dotted path that resolves to the candidate's file, redirect aliases included."""
    _, canonical = CANDIDATES[label]
    targets = {canonical}
    for legacy, real in _REDIRECT_MAP.items():
        if real == canonical:
            targets.add(legacy if legacy.startswith("bernstein.") else f"bernstein.core.{legacy}")
    return targets


def _importers_of(label: str) -> set[Path]:
    """Every file that imports `label`, by any of its resolvable dotted paths."""
    own_file, _ = CANDIDATES[label]
    targets = _import_targets(label)
    packages = {t.rsplit(".", 1)[0] for t in targets}
    module_name = own_file.stem
    dotted, from_package = _import_index()

    found: set[Path] = set()
    for target in targets:
        found |= {p for p in dotted.get(target, ()) if p != own_file}
    for package in packages:
        found |= {p for p in from_package.get((package, module_name), ()) if p != own_file}
    return found


def reachable_labels(importers: dict[str, set[Path]]) -> set[str]:
    """Candidates reachable from a caller outside the candidate set itself.

    Having an importer is not the same as being reachable: two dead modules
    that import each other each have one, and a scan that stops at "somebody
    imports it" reports both as live. Seed from callers whose file is not
    itself one of the candidates, then close over intra-candidate edges.
    """
    candidate_files = {path for path, _ in CANDIDATES.values()}
    reachable = {label for label, paths in importers.items() if any(p not in candidate_files for p in paths)}
    grew = True
    while grew:
        grew = False
        file_to_label = {path: label for label, (path, _) in CANDIDATES.items()}
        for label, paths in importers.items():
            if label in reachable:
                continue
            if any(file_to_label.get(p) in reachable for p in paths):
                reachable.add(label)
                grew = True
    return reachable


def _current_orphans() -> set[str]:
    importers = {label: _importers_of(label) for label in CANDIDATES}
    return set(importers) - reachable_labels(importers)


def test_every_compliance_module_has_a_non_test_importer() -> None:
    """The set of caller-less compliance modules may shrink, never grow.

    Load-bearing: on an unmodified tree this fails without the allowlist,
    because ``compliance_library`` (23 checks, 1,167 lines) has zero
    non-test importers - only two trailing comments in
    ``core/config/seed_parser.py`` name it. With ``KNOWN_ORPHANS`` in place
    the assertion passes today, documenting the gap instead of hiding it,
    and fails again the moment a fifth compliance module goes dark.
    """
    current = _current_orphans()

    appeared = sorted(current - KNOWN_ORPHANS)
    assert not appeared, (
        f"new caller-less compliance modules: {appeared}. Wire each one to a consumer "
        "that exists today, or delete the module together with its tests and its "
        "bernstein/core/__init__.py alias entry."
    )

    removed = sorted(KNOWN_ORPHANS - current)
    assert not removed, (
        f"{removed} now has a caller or is gone from the tree; strike it from "
        "KNOWN_ORPHANS so the list keeps shrinking."
    )


def test_a_wired_module_is_seen_through_its_legacy_alias() -> None:
    """`compliance_policies` is reachable only via its `core/__init__.py` redirect.

    Without redirect resolution the scan calls it an orphan, which is how a
    detector ends up demanding the deletion of a module `compliance_cmd.py`
    imports on every run of `bernstein compliance`.
    """
    assert _importers_of("compliance_policies")


def test_the_alias_table_alone_does_not_count_as_a_caller() -> None:
    """Otherwise every orphan listed in the redirect map reads as reachable.

    `compliance_library` has no alias entry at all - its name appears only
    in two trailing comments - so this checks one of the three orphans that
    *is* aliased (`hipaa`), which is the sharper case: the alias table names
    it, and it must still read as caller-less.
    """
    orphan = "hipaa"
    assert orphan in KNOWN_ORPHANS
    alias_table = (SRC / "core" / "__init__.py").read_text(encoding="utf-8")
    assert f'"{orphan}"' in alias_table, f"{orphan} is expected to be listed in the alias table"
    assert not _importers_of(orphan)


def test_a_mutually_importing_dead_cluster_is_still_orphaned() -> None:
    """Two dead candidates that import each other must not vouch for each other.

    This is the failure the "does anything import it?" shape cannot see, and
    it is how a whole dead corner of the tree survives a cleanup.
    """
    live_caller = SRC / "cli" / "commands" / "compliance_cmd.py"
    dead_a = CANDIDATES["compliance_library"][0]
    dead_b = CANDIDATES["hipaa"][0]
    importers = {
        "compliance_policies": {live_caller},
        "compliance_library": {dead_b},
        "hipaa": {dead_a},
    }
    assert reachable_labels(importers) == {"compliance_policies"}


def test_a_module_used_only_by_a_wired_module_is_reachable() -> None:
    """Positive control: intra-candidate edges do carry reachability."""
    live_caller = SRC / "cli" / "commands" / "compliance_cmd.py"
    helper = CANDIDATES["hipaa"][0]
    importers = {
        "compliance_policies": {live_caller},
        "hipaa": {CANDIDATES["compliance_policies"][0]},
    }
    assert helper == CANDIDATES["hipaa"][0]
    assert reachable_labels(importers) == {"compliance_policies", "hipaa"}
