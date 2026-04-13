"""Incremental type-checking that validates only changed files plus dependents.

Parses Python AST import statements to build a dependency graph, then
traces reverse dependencies to determine the minimal set of files that
need type-checking after a code change.  Runs pyright (or another
configurable command) on that scoped file list instead of the full
project, dramatically reducing type-check latency.

Typical use::

    scope = compute_typecheck_scope(["src/bernstein/core/foo.py"], Path("."))
    result = run_incremental_typecheck(scope, Path("."))
    if not result.passed:
        for error in result.errors:
            print(error)
"""

from __future__ import annotations

import ast
import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


def _empty_modules() -> dict[str, set[str]]:
    """Return an empty modules dict for dataclass default_factory."""
    return {}


@dataclass
class ImportGraph:
    """Dependency graph mapping each module to the modules it imports.

    Attributes:
        modules: Mapping from a module's relative file path to the set of
            relative file paths it imports.
    """

    modules: dict[str, set[str]] = field(default_factory=_empty_modules)


@dataclass(frozen=True)
class TypeCheckScope:
    """The set of files that need type-checking after a change.

    Attributes:
        changed_files: Files that were directly modified.
        dependent_files: Files that transitively depend on the changed files.
        total_files: Total number of Python files in the project.
        reduction_pct: Percentage reduction vs checking the full project.
    """

    changed_files: tuple[str, ...]
    dependent_files: tuple[str, ...]
    total_files: int
    reduction_pct: float


@dataclass(frozen=True)
class TypeCheckResult:
    """Outcome of running the type-checker on a scoped file set.

    Attributes:
        scope: The scope that was checked.
        errors: Tuple of error strings from the type-checker.
        passed: Whether the type-check passed with zero errors.
        duration_s: Wall-clock seconds taken.
    """

    scope: TypeCheckScope
    errors: tuple[str, ...]
    passed: bool
    duration_s: float


# ---------------------------------------------------------------------------
# AST import extraction
# ---------------------------------------------------------------------------

_STDLIB_TOP_LEVEL: frozenset[str] = frozenset(
    {
        "abc",
        "aifc",
        "argparse",
        "ast",
        "asyncio",
        "base64",
        "binascii",
        "bisect",
        "builtins",
        "calendar",
        "cgi",
        "cgitb",
        "cmd",
        "code",
        "codecs",
        "collections",
        "colorsys",
        "compileall",
        "concurrent",
        "configparser",
        "contextlib",
        "contextvars",
        "copy",
        "copyreg",
        "cProfile",
        "csv",
        "ctypes",
        "curses",
        "dataclasses",
        "datetime",
        "dbm",
        "decimal",
        "difflib",
        "dis",
        "distutils",
        "doctest",
        "email",
        "encodings",
        "enum",
        "errno",
        "faulthandler",
        "fcntl",
        "filecmp",
        "fileinput",
        "fnmatch",
        "fractions",
        "ftplib",
        "functools",
        "gc",
        "getopt",
        "getpass",
        "gettext",
        "glob",
        "grp",
        "gzip",
        "hashlib",
        "heapq",
        "hmac",
        "html",
        "http",
        "idlelib",
        "imaplib",
        "importlib",
        "inspect",
        "io",
        "ipaddress",
        "itertools",
        "json",
        "keyword",
        "lib2to3",
        "linecache",
        "locale",
        "logging",
        "lzma",
        "mailbox",
        "mailcap",
        "marshal",
        "math",
        "mimetypes",
        "mmap",
        "multiprocessing",
        "netrc",
        "numbers",
        "operator",
        "optparse",
        "os",
        "pathlib",
        "pdb",
        "pickle",
        "pickletools",
        "pipes",
        "pkgutil",
        "platform",
        "plistlib",
        "poplib",
        "posix",
        "posixpath",
        "pprint",
        "profile",
        "pstats",
        "pty",
        "pwd",
        "py_compile",
        "pyclbr",
        "pydoc",
        "queue",
        "quopri",
        "random",
        "re",
        "readline",
        "reprlib",
        "resource",
        "rlcompleter",
        "runpy",
        "sched",
        "secrets",
        "select",
        "selectors",
        "shelve",
        "shlex",
        "shutil",
        "signal",
        "site",
        "smtpd",
        "smtplib",
        "sndhdr",
        "socket",
        "socketserver",
        "sqlite3",
        "ssl",
        "stat",
        "statistics",
        "string",
        "stringprep",
        "struct",
        "subprocess",
        "sunau",
        "symtable",
        "sys",
        "sysconfig",
        "syslog",
        "tabnanny",
        "tarfile",
        "telnetlib",
        "tempfile",
        "termios",
        "test",
        "textwrap",
        "threading",
        "time",
        "timeit",
        "tkinter",
        "token",
        "tokenize",
        "tomllib",
        "trace",
        "traceback",
        "tracemalloc",
        "tty",
        "turtle",
        "turtledemo",
        "types",
        "typing",
        "unicodedata",
        "unittest",
        "urllib",
        "uu",
        "uuid",
        "venv",
        "warnings",
        "wave",
        "weakref",
        "webbrowser",
        "winreg",
        "winsound",
        "wsgiref",
        "xdrlib",
        "xml",
        "xmlrpc",
        "zipapp",
        "zipfile",
        "zipimport",
        "zlib",
        "_thread",
        "__future__",
    }
)


def _is_project_import(module_name: str) -> bool:
    """Return True if the import is a local/project import, not stdlib or third-party.

    Heuristic: reject known stdlib top-level names and anything starting
    with an underscore (private C extensions).  Everything else is treated
    as a potential project import that we later resolve against the file tree.
    """
    top = module_name.split(".", 1)[0]
    return top not in _STDLIB_TOP_LEVEL


def _collect_project_imports(tree: ast.Module) -> set[str]:
    """Extract project import module names from an AST tree."""
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_project_import(alias.name):
                    imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module and _is_project_import(module):
                imports.add(module)
    return imports


def _extract_imports_from_file(filepath: Path) -> set[str]:
    """Parse a Python file and return the set of dotted module names it imports.

    Only returns imports that pass the ``_is_project_import`` heuristic.
    Returns an empty set on parse/read errors.
    """
    try:
        source = filepath.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return set()

    try:
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError:
        return set()

    return _collect_project_imports(tree)


def _build_module_candidates(parts: list[str], project_root: Path) -> list[Path]:
    """Build candidate file paths for a dotted module name."""
    candidates: list[Path] = []
    for prefix in (project_root / "src", project_root):
        if len(parts) > 1:
            candidates.append(prefix / Path(*parts[:-1]) / f"{parts[-1]}.py")
        else:
            candidates.append(prefix / f"{parts[0]}.py")
        candidates.append(prefix / Path(*parts) / "__init__.py")
    return candidates


def _module_to_filepath(module_name: str, project_root: Path) -> str | None:
    """Resolve a dotted module name to a relative file path within the project.

    Checks both ``src/<module_path>.py`` and ``src/<module_path>/__init__.py``,
    as well as top-level paths without the ``src/`` prefix.
    """
    parts = module_name.split(".")
    for candidate in _build_module_candidates(parts, project_root):
        if candidate.is_file():
            try:
                return str(candidate.relative_to(project_root))
            except ValueError:
                continue
    return None


def _filepath_to_dotted(filepath: str) -> str:
    """Convert a relative .py filepath to a dotted module name.

    Strips leading ``src/`` if present and converts ``__init__.py`` to
    its package name.
    """
    p = filepath.replace("\\", "/")
    if p.startswith("src/"):
        p = p[4:]
    if p.endswith("/__init__.py"):
        p = p[:-12]
    elif p.endswith(".py"):
        p = p[:-3]
    return p.replace("/", ".")


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------


def build_import_graph(project_root: Path) -> ImportGraph:
    """AST-parse all .py files under *project_root* and build a dependency graph.

    Each node in the graph is a relative file path.  Edges point from a file
    to the files it imports.  Only project-internal imports (resolved to
    actual on-disk files) are included.

    Args:
        project_root: Root directory of the Python project.

    Returns:
        An ``ImportGraph`` mapping each ``.py`` file to the set of files
        it imports.
    """
    graph = ImportGraph()
    py_files: list[Path] = sorted(project_root.rglob("*.py"))

    for py_file in py_files:
        try:
            rel_path = str(py_file.relative_to(project_root))
        except ValueError:
            continue

        # Skip hidden directories, __pycache__, etc.
        if any(part.startswith(".") or part == "__pycache__" for part in Path(rel_path).parts):
            continue

        raw_imports = _extract_imports_from_file(py_file)
        resolved: set[str] = set()
        for module_name in raw_imports:
            target = _module_to_filepath(module_name, project_root)
            if target is not None and target != rel_path:
                resolved.add(target)

        graph.modules[rel_path] = resolved

    return graph


# ---------------------------------------------------------------------------
# Reverse dependency tracing
# ---------------------------------------------------------------------------


def find_dependents(changed_files: list[str], graph: ImportGraph) -> list[str]:
    """Trace reverse dependencies to find all files affected by the changes.

    Starting from *changed_files*, walks the import graph in reverse to
    find every file that transitively imports any of the changed files.

    Args:
        changed_files: Relative file paths that were modified.
        graph: The project's import dependency graph.

    Returns:
        Sorted list of dependent file paths (excluding the changed files
        themselves).
    """
    # Build reverse graph: target → set of importers
    reverse: dict[str, set[str]] = {}
    for source, targets in graph.modules.items():
        for target in targets:
            reverse.setdefault(target, set()).add(source)

    changed_set = set(changed_files)
    visited: set[str] = set(changed_files)
    worklist = list(changed_files)

    while worklist:
        current = worklist.pop()
        for importer in reverse.get(current, set()):
            if importer not in visited:
                visited.add(importer)
                worklist.append(importer)

    dependents = sorted(visited - changed_set)
    return dependents


# ---------------------------------------------------------------------------
# Scope computation
# ---------------------------------------------------------------------------


def _count_py_files(project_root: Path) -> int:
    """Count all .py files in the project, excluding hidden dirs and __pycache__."""
    count = 0
    for py_file in project_root.rglob("*.py"):
        try:
            rel = py_file.relative_to(project_root)
        except ValueError:
            continue
        if any(part.startswith(".") or part == "__pycache__" for part in rel.parts):
            continue
        count += 1
    return count


def compute_typecheck_scope(
    changed_files: list[str],
    project_root: Path,
) -> TypeCheckScope:
    """Return a ``TypeCheckScope`` with changed files plus all dependents.

    Builds the import graph, traces reverse dependencies, and computes the
    percentage reduction compared to checking the full project.

    Args:
        changed_files: Relative paths of changed ``.py`` files.
        project_root: Root directory of the Python project.

    Returns:
        A frozen ``TypeCheckScope`` describing the minimal check set.
    """
    graph = build_import_graph(project_root)
    dependents = find_dependents(changed_files, graph)
    total = _count_py_files(project_root)

    scoped_count = len(changed_files) + len(dependents)
    reduction = round((1.0 - scoped_count / total) * 100.0, 1) if total > 0 else 0.0

    return TypeCheckScope(
        changed_files=tuple(changed_files),
        dependent_files=tuple(dependents),
        total_files=total,
        reduction_pct=max(reduction, 0.0),
    )


# ---------------------------------------------------------------------------
# Type-checker runner
# ---------------------------------------------------------------------------


def run_incremental_typecheck(
    scope: TypeCheckScope,
    project_root: Path,
    command: str = "pyright",
) -> TypeCheckResult:
    """Run a type-checker on the scoped file list.

    Executes the *command* (default ``pyright``) with the scoped files
    as arguments.  If the scope is empty the check is trivially passed.

    Args:
        scope: The ``TypeCheckScope`` to check.
        project_root: Root directory used as cwd for the subprocess.
        command: Type-checker executable (default ``"pyright"``).

    Returns:
        A frozen ``TypeCheckResult`` with errors and timing.
    """
    files_to_check = list(scope.changed_files) + list(scope.dependent_files)
    if not files_to_check:
        return TypeCheckResult(
            scope=scope,
            errors=(),
            passed=True,
            duration_s=0.0,
        )

    cmd = [command, *files_to_check]
    start = time.monotonic()

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=300,
        )
    except FileNotFoundError:
        elapsed = time.monotonic() - start
        return TypeCheckResult(
            scope=scope,
            errors=(f"Type-checker command not found: {command}",),
            passed=False,
            duration_s=round(elapsed, 3),
        )
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - start
        return TypeCheckResult(
            scope=scope,
            errors=("Type-checker timed out after 300s",),
            passed=False,
            duration_s=round(elapsed, 3),
        )

    elapsed = time.monotonic() - start

    errors: list[str] = []
    output = proc.stdout or ""
    for line in output.splitlines():
        stripped = line.strip()
        if stripped and "error" in stripped.lower():
            errors.append(stripped)

    passed = proc.returncode == 0

    return TypeCheckResult(
        scope=scope,
        errors=tuple(errors),
        passed=passed,
        duration_s=round(elapsed, 3),
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_scope_summary(scope: TypeCheckScope) -> str:
    """Render a Markdown summary of the type-check scope.

    Args:
        scope: The ``TypeCheckScope`` to summarise.

    Returns:
        A Markdown-formatted string showing changed files, dependent
        files, totals, and reduction percentage.
    """
    lines: list[str] = []
    lines.append("## Incremental Type-Check Scope")
    lines.append("")

    scoped_count = len(scope.changed_files) + len(scope.dependent_files)
    lines.append(f"**Scoped files:** {scoped_count} / {scope.total_files} ({scope.reduction_pct:.1f}% reduction)")
    lines.append("")

    if scope.changed_files:
        lines.append(f"### Changed ({len(scope.changed_files)})")
        lines.append("")
        for f in scope.changed_files:
            module = _filepath_to_dotted(f)
            lines.append(f"- `{f}` ({module})")
        lines.append("")

    if scope.dependent_files:
        lines.append(f"### Dependents ({len(scope.dependent_files)})")
        lines.append("")
        for f in scope.dependent_files:
            module = _filepath_to_dotted(f)
            lines.append(f"- `{f}` ({module})")
        lines.append("")

    if not scope.changed_files and not scope.dependent_files:
        lines.append("No files in scope.")
        lines.append("")

    return "\n".join(lines)
