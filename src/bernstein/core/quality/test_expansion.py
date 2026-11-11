"""Static module reachability with a content-scoped, process-local import index.

This is a module import check, not evidence that a test invokes a symbol.
The index payload is the existing test-impact compatibility map. No gate
verdict or new persisted cache format is introduced.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Any

from bernstein.core.quality.test_impact import (
    _expand_transitive_modules,
    _path_to_module,
    build_compat_dep_map,
    discover_module_aliases,
    extract_project_imports,
    resolve_module_aliases,
)

_EXCLUDED_DIRS = frozenset({"build", "dist", "node_modules", "__pycache__"})


def _project_sources(base: Path, *, flat: bool = False) -> list[Path]:
    """Prune non-project trees before enumeration, hashing or AST parsing."""
    files: list[Path] = []
    if not base.is_dir():
        return files
    for directory, names, filenames in base.walk():
        names[:] = [
            name
            for name in names
            if not name.startswith(".")
            and name not in _EXCLUDED_DIRS
            and not (directory / name / "pyvenv.cfg").is_file()
            and not (flat and name in {"tests", "test"})
        ]
        files.extend(
            directory / name
            for name in filenames
            if name.endswith(".py") and not (flat and (name.startswith("test_") or name.endswith("_test.py")))
        )
    return sorted(files)


@lru_cache(maxsize=8)
def _build_index(
    root: Path,
    src_root: Path,
    test_dirs: tuple[Path, ...],
    prefixes: frozenset[str],
    sources: tuple[Path, ...],
    extras: tuple[Path, ...],
    fingerprint: tuple[tuple[str, str], ...],
) -> tuple[dict[str, Any], dict[str, str]]:
    # The fingerprint is part of the cache key, including every path and its
    # bytes. New/deleted sources, unchanged bridges and test edits invalidate
    # the graph even when the task's own changed files are identical.
    dep_map = build_compat_dep_map(
        root, src_root, list(test_dirs), set(prefixes), list(extras), source_paths=list(sources)
    )
    return dep_map, discover_module_aliases(src_root, source_files=list(sources))


def find_uncovered_source_modules(root: Path, source_files: list[str], changed_files: list[str]) -> list[str]:
    """Return changed modules with no static import path from changed tests.

    Warm calls reuse only the parsed graph, after hashing its full source/test
    scope. Required/advisory verdicts remain the runner's responsibility.
    Dynamic imports and symbol execution are outside this static check.
    """
    root = root.resolve()
    changed_tests = [
        f
        for f in changed_files
        if f.endswith(".py") and (Path(f).name.startswith("test_") or Path(f).name.endswith("_test.py"))
    ]
    src_root = root / "src" if (root / "src").is_dir() else root
    sources = _project_sources(src_root, flat=src_root == root)
    prefixes = {path.relative_to(src_root).parts[0].removesuffix(".py") for path in sources}
    extra_sources: set[Path] = set()
    for rel in source_files:
        path = root / rel
        if path.is_relative_to(src_root):
            continue
        parts = Path(rel).parts
        if len(parts) > 1:
            prefixes.add(parts[0])
            extra_sources.update(_project_sources(root / parts[0]))
        else:
            prefixes.add(path.stem)
            extra_sources.add(path)
    root_modules = {
        path.name if path.is_dir() else path.stem
        for path in root.iterdir()
        if path.name not in _EXCLUDED_DIRS | {"src", "tests"}
        and not path.name.startswith(".")
        and (path.is_dir() or path.suffix == ".py")
    }
    for rel in changed_tests:
        for imported in extract_project_imports(root / rel, root_modules - prefixes):
            top = imported.split(".", 1)[0]
            root_path = root / top
            if root_path.is_dir():
                extra_sources.update(_project_sources(root_path))
            elif root_path.with_suffix(".py").is_file():
                extra_sources.add(root_path.with_suffix(".py"))
            prefixes.add(top)
    test_dirs = tuple(sorted({(root / f).parent for f in changed_tests if (root / f).parent != root}))
    indexed_tests = {test for directory in test_dirs if directory.is_dir() for test in directory.rglob("test_*.py")}
    fingerprint = tuple(
        (str(path), hashlib.sha256(path.read_bytes()).hexdigest())
        for path in sorted(set(sources) | extra_sources | indexed_tests)
        if path.is_file()
    )
    dep_map, aliases = _build_index(
        root, src_root, test_dirs, frozenset(prefixes), tuple(sources), tuple(sorted(extra_sources)), fingerprint
    )
    source_imports = dep_map["source_imports"]
    test_deps = dep_map["test_deps"]
    changed_imports: set[str] = set()
    for rel in changed_tests:
        if rel in test_deps:
            changed_imports.update(test_deps[rel]["imports"])
        else:
            changed_imports.update(
                resolve_module_aliases(
                    extract_project_imports(
                        root / rel, prefixes, importable_modules=set(source_imports) | set(aliases)
                    ),
                    aliases,
                )
            )
    uncovered: list[str] = []
    for rel in source_files:
        path = root / rel
        module = _path_to_module(path, src_root if path.is_relative_to(src_root) else root)
        roots = {module}
        if path.name == "__init__.py":
            roots.update(name for name in source_imports if name.startswith(f"{module}."))
        if not changed_imports.intersection(_expand_transitive_modules(roots, source_imports)):
            uncovered.append(rel)
    return uncovered
