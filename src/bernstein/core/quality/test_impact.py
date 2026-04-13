"""Incremental test impact analysis for quality gates and test tooling.

Provides a typed analyzer used by the quality gate runner plus compatibility
helpers for the legacy ``scripts/test_impact.py`` CLI contract.
"""

from __future__ import annotations

import ast
import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from typing_extensions import TypedDict

logger = logging.getLogger(__name__)

_ANALYZER_CACHE_VERSION = "2"
_COMPAT_CACHE_VERSION = "1"


# Shared cast-type constants to avoid string duplication (Sonar S1192).
_CAST_DICT_STR_OBJ = "dict[str, object]"


class AnalyzerCacheData(TypedDict):
    """Typed payload for persisted analyzer cache data."""

    graph: dict[str, list[str]]
    reverse: dict[str, list[str]]
    source_imports: dict[str, list[str]]
    all_tests: list[str]


def _file_hash(path: Path) -> str:
    """Return a short content hash for a file."""
    return hashlib.sha1(path.read_bytes(), usedforsecurity=False).hexdigest()[:16]


def _iter_project_packages(src_root: Path) -> set[str]:
    """Return top-level import package names under ``src_root``."""
    packages: set[str] = set()
    if not src_root.exists():
        return packages
    for child in src_root.iterdir():
        if child.is_dir():
            packages.add(child.name)
    return packages


def _path_to_module(path: Path, src_root: Path) -> str:
    """Map a source file path to its dotted module name."""
    try:
        rel = path.relative_to(src_root)
    except ValueError:
        return path.as_posix()
    parts = list(rel.parts)
    if not parts:
        return ""
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    elif parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]
    return ".".join(parts)


def _normalize_string_list(raw: object) -> list[str]:
    """Return string items from a JSON-compatible list value."""
    if not isinstance(raw, list):
        return []
    items = cast("list[object]", raw)
    return [item for item in items if isinstance(item, str)]


def _normalize_mapping_list(raw: object) -> dict[str, list[str]]:
    """Return a typed ``dict[str, list[str]]`` mapping from JSON data."""
    if not isinstance(raw, dict):
        return {}
    raw_dict = cast("dict[object, object]", raw)
    normalized: dict[str, list[str]] = {}
    for key, value in raw_dict.items():
        if isinstance(key, str):
            normalized[key] = _normalize_string_list(value)
    return normalized


def extract_project_imports(path: Path, package_prefixes: set[str]) -> set[str]:
    """Extract imported project module names from a Python file."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return set()

    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".", 1)[0]
                if top in package_prefixes:
                    imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if not module:
                continue
            top = module.split(".", 1)[0]
            if top in package_prefixes:
                imports.add(module)
    return imports


def build_compat_dep_map(
    root: Path,
    src_root: Path,
    test_dirs: list[Path],
    package_prefixes: set[str] | None = None,
) -> dict[str, Any]:
    """Build the legacy dependency-map shape used by ``scripts/test_impact.py``."""
    prefixes = package_prefixes or _iter_project_packages(src_root)
    test_deps: dict[str, dict[str, Any]] = {}
    for test_dir in test_dirs:
        if not test_dir.exists():
            continue
        for test_file in sorted(test_dir.glob("test_*.py")):
            rel = test_file.relative_to(root).as_posix()
            test_deps[rel] = {
                "hash": _file_hash(test_file),
                "imports": sorted(extract_project_imports(test_file, prefixes)),
            }

    source_imports: dict[str, dict[str, Any]] = {}
    if src_root.exists():
        for src_file in sorted(src_root.rglob("*.py")):
            module = _path_to_module(src_file, src_root)
            if not module:
                continue
            source_imports[module] = {
                "hash": _file_hash(src_file),
                "imports": sorted(extract_project_imports(src_file, prefixes)),
            }

    return {
        "version": _COMPAT_CACHE_VERSION,
        "test_deps": test_deps,
        "source_imports": source_imports,
    }


def compat_cache_is_fresh(
    cached: dict[str, Any],
    *,
    root: Path,
    src_root: Path,
    test_dirs: list[Path],
) -> bool:
    """Return whether a legacy dependency map still matches on-disk files."""
    test_deps_raw = cached.get("test_deps")
    source_imports_raw = cached.get("source_imports")
    if cached.get("version") != _COMPAT_CACHE_VERSION:
        return False
    test_deps = cast(_CAST_DICT_STR_OBJ, test_deps_raw) if isinstance(test_deps_raw, dict) else {}
    source_imports = cast(_CAST_DICT_STR_OBJ, source_imports_raw) if isinstance(source_imports_raw, dict) else {}

    for test_dir in test_dirs:
        if not test_dir.exists():
            continue
        for test_file in test_dir.glob("test_*.py"):
            rel = test_file.relative_to(root).as_posix()
            entry = test_deps.get(rel)
            if not isinstance(entry, dict):
                return False
            entry_dict = cast(_CAST_DICT_STR_OBJ, entry)
            if entry_dict.get("hash") != _file_hash(test_file):
                return False

    if src_root.exists():
        for src_file in src_root.rglob("*.py"):
            module = _path_to_module(src_file, src_root)
            entry = source_imports.get(module)
            if not isinstance(entry, dict):
                return False
            entry_dict = cast(_CAST_DICT_STR_OBJ, entry)
            if entry_dict.get("hash") != _file_hash(src_file):
                return False

    return True


def compat_get_affected_tests(
    changed_files: list[str],
    dep_map: dict[str, Any],
    *,
    root: Path,
    src_root: Path,
) -> list[Path]:
    """Return affected test file paths for the legacy script contract."""
    test_deps_raw = dep_map.get("test_deps")
    source_imports_raw = dep_map.get("source_imports")
    test_deps = cast(_CAST_DICT_STR_OBJ, test_deps_raw) if isinstance(test_deps_raw, dict) else {}
    source_imports = cast(_CAST_DICT_STR_OBJ, source_imports_raw) if isinstance(source_imports_raw, dict) else {}

    if any("conftest.py" in changed for changed in changed_files):
        return sorted(root / rel for rel in test_deps)

    module_to_tests: dict[str, set[str]] = {}
    for test_rel, entry in test_deps.items():
        if not isinstance(entry, dict):
            continue
        entry_dict = cast(_CAST_DICT_STR_OBJ, entry)
        for module in _normalize_string_list(entry_dict.get("imports", [])):
            module_to_tests.setdefault(module, set()).add(str(test_rel))

    changed_modules: set[str] = set()
    for rel_path in changed_files:
        file_path = root / rel_path
        if file_path.suffix == ".py" and file_path.is_relative_to(src_root):
            changed_modules.add(_path_to_module(file_path, src_root))

    all_affected = set(changed_modules)
    worklist = set(changed_modules)
    while worklist:
        current = worklist.pop()
        for module, info in source_imports.items():
            if not isinstance(info, dict):
                continue
            info_dict = cast(_CAST_DICT_STR_OBJ, info)
            imports = _normalize_string_list(info_dict.get("imports", []))
            if current in imports and module not in all_affected:
                all_affected.add(str(module))
                worklist.add(str(module))

    affected_tests: set[str] = set()
    for rel_path in changed_files:
        file_path = root / rel_path
        if file_path.suffix == ".py" and file_path.name.startswith("test_"):
            rel = file_path.relative_to(root).as_posix()
            if rel in test_deps:
                affected_tests.add(rel)

    for module in all_affected:
        affected_tests.update(module_to_tests.get(module, set()))
        parts = module.split(".")
        for index in range(1, len(parts)):
            affected_tests.update(module_to_tests.get(".".join(parts[:index]), set()))

    return sorted(root / rel for rel in affected_tests)


@dataclass(frozen=True)
class TestMapping:
    """Maps a changed source file to affected test files."""

    source_file: str
    test_files: list[str]
    reason: str


@dataclass(frozen=True)
class ImpactAnalysis:
    """Result of analyzing impacted tests for a changed-file set."""

    changed_files: list[str]
    affected_tests: list[str]
    mappings: list[TestMapping]
    coverage_pct: float
    fallback_used: bool


class TestImpactAnalyzer:
    """Determine which tests are affected by a set of changed files."""

    def __init__(
        self,
        project_root: Path,
        *,
        cache_path: Path | None = None,
        src_root: Path | None = None,
        test_dirs: list[Path] | None = None,
    ) -> None:
        self._root = project_root
        self._src_root = src_root or (project_root / "src")
        self._test_dirs = test_dirs or [project_root / "tests"]
        self._cache_path = cache_path or (project_root / ".sdd" / "cache" / "test_impact_index.json")
        self._package_prefixes = _iter_project_packages(self._src_root)
        self._graph: dict[str, set[str]] = {}
        self._reverse: dict[str, set[str]] = {}
        self._source_imports: dict[str, set[str]] = {}
        self._all_tests: set[str] = set()
        self._built = False

    def build_index(self, *, force: bool = False) -> None:
        """Build or load the source-to-test mapping index."""
        if self._built and not force:
            return
        if not force:
            cached = self._load_cache()
            if cached is not None:
                self._graph = {key: set(value) for key, value in cached["graph"].items()}
                self._reverse = {key: set(value) for key, value in cached["reverse"].items()}
                self._source_imports = {key: set(value) for key, value in cached["source_imports"].items()}
                self._all_tests = set(cached["all_tests"])
                self._built = True
                return

        graph: dict[str, set[str]] = {}
        reverse: dict[str, set[str]] = {}
        source_imports: dict[str, set[str]] = {}
        all_tests: set[str] = set()

        for test_file in self._discover_tests():
            rel = test_file.relative_to(self._root).as_posix()
            all_tests.add(rel)
            imports = self._parse_test_imports(test_file)
            for module in imports:
                graph.setdefault(module, set()).add(rel)
                reverse.setdefault(rel, set()).add(module)

        if self._src_root.exists():
            for source_file in sorted(self._src_root.rglob("*.py")):
                module = _path_to_module(source_file, self._src_root)
                if not module:
                    continue
                source_imports[module] = self._parse_source_imports(source_file)
                for test_file in self._name_based_mapping(source_file.relative_to(self._root).as_posix()):
                    graph.setdefault(module, set()).add(test_file)
                    reverse.setdefault(test_file, set()).add(module)
                    all_tests.add(test_file)

        self._graph = graph
        self._reverse = reverse
        self._source_imports = source_imports
        self._all_tests = all_tests
        self._persist_cache()
        self._built = True

    def analyze(self, changed_files: list[str]) -> ImpactAnalysis:
        """Analyze a changed-file set and return impacted tests."""
        self.build_index()
        normalized = sorted({Path(path).as_posix() for path in changed_files})
        if not normalized:
            return ImpactAnalysis(
                changed_files=[],
                affected_tests=[],
                mappings=[],
                coverage_pct=0.0,
                fallback_used=False,
            )

        all_tests = sorted(self._all_tests)
        if any(path.endswith("conftest.py") and path.startswith("tests/") for path in normalized):
            return ImpactAnalysis(
                changed_files=normalized,
                affected_tests=all_tests,
                mappings=[TestMapping(source_file="tests/conftest.py", test_files=all_tests, reason="all")],
                coverage_pct=100.0,
                fallback_used=True,
            )

        affected: set[str] = set()
        mappings: list[TestMapping] = []
        changed_sources = [
            path for path in normalized if path.endswith(".py") and (self._root / path).is_relative_to(self._src_root)
        ]
        covered_sources = 0

        for rel_path in normalized:
            file_path = self._root / rel_path
            if file_path.name.startswith("test_") and file_path.suffix == ".py" and rel_path.startswith("tests/"):
                affected.add(rel_path)
                mappings.append(TestMapping(source_file=rel_path, test_files=[rel_path], reason="direct_import"))

        for rel_path in changed_sources:
            file_path = self._root / rel_path
            module = _path_to_module(file_path, self._src_root)
            if not module:
                continue

            module_tests: set[str] = set()
            reason = "direct_import"
            if file_path.name == "__init__.py":
                reason = "all"
                module_tests.update(self._tests_for_package(module))
            else:
                impacted_modules = self._expand_impacted_modules({module})
                for impacted_module in impacted_modules:
                    tests = self._graph.get(impacted_module, set())
                    if tests:
                        if impacted_module != module:
                            reason = "transitive"
                        module_tests.update(tests)

            if module_tests:
                covered_sources += 1
                test_list = sorted(module_tests)
                affected.update(test_list)
                mappings.append(TestMapping(source_file=rel_path, test_files=test_list, reason=reason))

        if changed_sources and not affected:
            return ImpactAnalysis(
                changed_files=normalized,
                affected_tests=all_tests,
                mappings=[],
                coverage_pct=0.0,
                fallback_used=True,
            )

        coverage_pct = 0.0
        if changed_sources:
            coverage_pct = round((covered_sources / len(changed_sources)) * 100.0, 2)
        elif affected:
            coverage_pct = 100.0

        return ImpactAnalysis(
            changed_files=normalized,
            affected_tests=sorted(affected),
            mappings=mappings,
            coverage_pct=coverage_pct,
            fallback_used=False,
        )

    def _discover_tests(self) -> list[Path]:
        """Find test files in configured test directories."""
        tests: list[Path] = []
        for test_dir in self._test_dirs:
            if not test_dir.exists():
                continue
            tests.extend(sorted(path for path in test_dir.rglob("test_*.py") if path.is_file()))
        return tests

    def _parse_test_imports(self, test_file: Path) -> set[str]:
        """Parse source dependencies imported by a test file."""
        return extract_project_imports(test_file, self._package_prefixes)

    def _parse_source_imports(self, source_file: Path) -> set[str]:
        """Parse project imports used by a source file."""
        return extract_project_imports(source_file, self._package_prefixes)

    def _name_based_mapping(self, source_rel: str) -> list[str]:
        """Map a source file to likely test files by naming convention."""
        source_path = Path(source_rel)
        if source_path.name == "__init__.py":
            return []
        stem = source_path.stem
        matches: set[str] = set()
        for test_file in self._discover_tests():
            if test_file.name == f"test_{stem}.py":
                matches.add(test_file.relative_to(self._root).as_posix())
        return sorted(matches)

    def _snapshot(self) -> dict[str, int]:
        """Return an mtime snapshot used for cache invalidation."""
        snapshot: dict[str, int] = {}
        paths: list[Path] = []
        if self._src_root.exists():
            paths.extend(sorted(self._src_root.rglob("*.py")))
        for test_dir in self._test_dirs:
            if test_dir.exists():
                paths.extend(sorted(test_dir.rglob("*.py")))
        for path in paths:
            try:
                snapshot[path.relative_to(self._root).as_posix()] = path.stat().st_mtime_ns
            except OSError:
                continue
        return snapshot

    def _load_cache(self) -> AnalyzerCacheData | None:
        """Load a previously persisted analyzer cache when still fresh."""
        if not self._cache_path.exists():
            return None
        try:
            raw: object = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict):
            return None
        data = cast(_CAST_DICT_STR_OBJ, raw)
        if data.get("version") != _ANALYZER_CACHE_VERSION:
            return None
        snapshot = data.get("snapshot")
        if not isinstance(snapshot, dict) or snapshot != self._snapshot():
            return None
        graph = _normalize_mapping_list(data.get("graph", {}))
        reverse = _normalize_mapping_list(data.get("reverse", {}))
        source_imports = _normalize_mapping_list(data.get("source_imports", {}))
        all_tests = _normalize_string_list(data.get("all_tests", []))
        return {
            "graph": graph,
            "reverse": reverse,
            "source_imports": source_imports,
            "all_tests": all_tests,
        }

    def _persist_cache(self) -> None:
        """Persist the analyzer cache."""
        payload = {
            "version": _ANALYZER_CACHE_VERSION,
            "snapshot": self._snapshot(),
            "graph": {key: sorted(value) for key, value in self._graph.items()},
            "reverse": {key: sorted(value) for key, value in self._reverse.items()},
            "source_imports": {key: sorted(value) for key, value in self._source_imports.items()},
            "all_tests": sorted(self._all_tests),
        }
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    def _expand_impacted_modules(self, start_modules: set[str]) -> set[str]:
        """Expand changed modules to transitive reverse dependencies."""
        affected = set(start_modules)
        worklist = set(start_modules)
        while worklist:
            current = worklist.pop()
            for module, imports in self._source_imports.items():
                if current in imports and module not in affected:
                    affected.add(module)
                    worklist.add(module)
        return affected

    def _module_to_source_path(self, module: str) -> str | None:
        """Convert a dotted module name back to its relative source file path.

        Args:
            module: Dotted module name, e.g. ``"bernstein.core.foo"``.

        Returns:
            Relative posix path if the file exists on disk, else ``None``.
        """
        parts = module.split(".")
        if not parts:
            return None
        # Try regular module file first: bernstein.core.foo -> src/bernstein/core/foo.py
        module_file = self._src_root.joinpath(*parts[:-1], f"{parts[-1]}.py")
        # Try package init: bernstein.core -> src/bernstein/core/__init__.py
        init_file = self._src_root.joinpath(*parts, "__init__.py")
        for candidate in (module_file, init_file):
            if candidate.exists():
                try:
                    return candidate.relative_to(self._root).as_posix()
                except ValueError:
                    pass
        return None

    def get_dependent_source_files(self, changed_files: list[str]) -> list[str]:
        """Return source files that transitively depend on the changed files.

        Given a list of changed source file paths, returns the file paths of
        all source files that import (directly or transitively) any of those
        files, plus the original changed files themselves.  Use this to expand
        the type-check scope so that callers of modified modules are validated.

        Args:
            changed_files: Relative posix paths of changed source files.

        Returns:
            Sorted, deduplicated list of relative paths including changed
            files and all transitive importers found in the source index.
            Falls back to returning just the original changed files when no
            dependency information is available.
        """
        self.build_index()
        if not self._source_imports:
            return sorted(changed_files)

        changed_modules: set[str] = set()
        for rel_path in changed_files:
            if not rel_path.endswith(".py"):
                continue
            file_path = self._root / rel_path
            try:
                if not file_path.is_relative_to(self._src_root):
                    continue
            except ValueError:
                continue
            module = _path_to_module(file_path, self._src_root)
            if module:
                changed_modules.add(module)

        if not changed_modules:
            return sorted(changed_files)

        all_affected = self._expand_impacted_modules(changed_modules)

        result: set[str] = set(changed_files)
        for module in all_affected:
            path = self._module_to_source_path(module)
            if path is not None:
                result.add(path)

        return sorted(result)

    def _tests_for_package(self, package_prefix: str) -> set[str]:
        """Return tests mapped to any module inside a package prefix."""
        tests: set[str] = set()
        for module, module_tests in self._graph.items():
            if module == package_prefix or module.startswith(f"{package_prefix}."):
                tests.update(module_tests)
        return tests
