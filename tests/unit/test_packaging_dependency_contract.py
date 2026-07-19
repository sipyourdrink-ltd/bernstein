"""Runtime-dependency contract for the ``packaging`` distribution.

Three shipped modules import ``packaging.version`` on operator happy paths:

* ``bernstein.cli.commands.doctor_cmd.check_canary_last_green`` - runs on
  every ``bernstein doctor`` invocation,
* ``bernstein.adapters.security_floor`` - adapter minimum-safe-version gate,
* ``bernstein.adapters.advisories`` - adapter advisory surface.

``packaging`` is only ever pulled into the lockfile transitively by dev/test
tooling (pytest, pip-audit, e2b, huggingface-hub, ...), none of which is a
runtime dependency of the wheel. A clean ``uv tool install bernstein`` therefore
ships *without* ``packaging``, and ``bernstein doctor`` crashes with
``ModuleNotFoundError: No module named 'packaging'`` instead of printing a
pre-flight report.

This test pins the contract: if source imports ``packaging`` at runtime, the
distribution must be a declared direct dependency so the resolver installs it.
It is derived from the source (not a hand-maintained allowlist) so it neither
fires spuriously if the imports are removed nor silently rots.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "src" / "bernstein"

# ``packaging`` at a top-level module boundary: ``import packaging`` /
# ``import packaging.version`` / ``from packaging.version import ...``. A bare
# substring match would false-positive on ``bernstein.core.skills.packaging``
# (a first-party subpackage), so the pattern anchors on the import keyword and
# a word boundary that the dotted first-party path never satisfies.
_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+packaging(?:\.\w+)*\s+import\b|import\s+packaging(?:\.\w+)*)",
    re.MULTILINE,
)


def _source_files_importing_packaging() -> list[Path]:
    hits: list[Path] = []
    for path in _SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if _IMPORT_RE.search(text):
            hits.append(path)
    return hits


def _declared_runtime_distributions() -> set[str]:
    with (_REPO / "pyproject.toml").open("rb") as fh:
        deps = tomllib.load(fh)["project"]["dependencies"]
    names: set[str] = set()
    for spec in deps:
        # Requirement grammar: name then optional extras/version/marker. The
        # name runs until the first separator character.
        name = re.split(r"[\s<>=!~;\[\(]", spec, maxsplit=1)[0]
        # PEP 503 normalisation so ``Packaging`` / ``packaging`` compare equal.
        names.add(re.sub(r"[-_.]+", "-", name).lower())
    return names


def test_packaging_is_a_declared_runtime_dependency() -> None:
    importers = _source_files_importing_packaging()
    assert importers, (
        "expected at least one runtime module to import ``packaging``; if the "
        "imports were intentionally removed, delete this contract test too."
    )
    declared = _declared_runtime_distributions()
    assert "packaging" in declared, (
        "src/bernstein imports ``packaging`` at runtime (e.g. "
        + ", ".join(sorted(str(p.relative_to(_REPO)) for p in importers))
        + ") but ``packaging`` is not in [project].dependencies. A clean "
        "install ships without it and ``bernstein doctor`` crashes with "
        "ModuleNotFoundError. Add ``packaging`` to pyproject dependencies."
    )
