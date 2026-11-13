"""Spellcheck scope around the vendored CycloneDX schemas.

``tests/fixtures/cyclonedx/`` holds verbatim copies of the official CycloneDX
JSON schemas so the AI-BOM / SBOM tests validate emitted documents offline. The
files are third-party artifacts reproduced byte-for-byte; their provenance is
the digests recorded in the fixture README.

The spelling gate reads the upstream wording as misspellings: three SPDX
licence identifiers in ``spdx.schema.json`` that abbreviate a legal term, plus
the schema's own variant spelling of an English word in
``bom-1.7.schema.json``. (The offending tokens are deliberately not reproduced
in this file -- quoting them verbatim re-triggers the same gate.) None of them
is a typo, and none can be "corrected" here: rewriting a licence identifier
fabricates an identifier that does not exist upstream, and any edit at all
falsifies the digest that proves the file is what it claims to be.

So the schemas are excluded from the scan. That exclusion is deliberately
file-scoped: the human-authored siblings beside them (the provenance README and
the ``__init__.py`` validator helper) stay *inside* the scope, so this file also
guards against the exclusion widening into a hole in prose coverage.
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from fnmatch import fnmatch
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG = REPO_ROOT / "typos.toml"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "cyclonedx"
README = FIXTURE_DIR / "README.md"

#: ``| `bom-1.7.schema.json` | `7330…` |`` in the fixture README's digest table.
_README_DIGEST_ROW = re.compile(r"^\|\s*`(?P<name>[^`]+)`\s*\|\s*`(?P<digest>[0-9a-f]{64})`\s*\|\s*$", re.MULTILINE)


def _exclude_patterns() -> list[str]:
    data = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
    files = data.get("files", {})
    patterns = files.get("extend-exclude", [])
    assert isinstance(patterns, list), "files.extend-exclude must be a list"
    return [str(p) for p in patterns]


def _is_excluded(path: Path) -> bool:
    rel = path.relative_to(REPO_ROOT).as_posix()
    return any(fnmatch(rel, pattern) for pattern in _exclude_patterns())


def _readme_digests() -> dict[str, str]:
    return {m.group("name"): m.group("digest") for m in _README_DIGEST_ROW.finditer(README.read_text(encoding="utf-8"))}


def test_vendored_schemas_are_excluded_from_the_spellcheck() -> None:
    """Every vendored schema is upstream content, not our prose."""
    schemas = sorted(FIXTURE_DIR.glob("*.schema.json"))

    assert schemas, f"no vendored schemas found under {FIXTURE_DIR}"
    for path in schemas:
        assert _is_excluded(path), f"{path.name} would be spellchecked; its upstream wording cannot be edited"


def test_the_human_authored_siblings_stay_spellchecked() -> None:
    """The exclusion must not widen onto the prose and code beside the schemas."""
    for name in ("README.md", "__init__.py"):
        path = FIXTURE_DIR / name
        assert path.is_file(), f"{path} is missing"
        assert not _is_excluded(path), f"{name} is prose-bearing and must stay in the spellcheck scope"


def test_every_vendored_schema_matches_its_recorded_digest() -> None:
    """Pin why the exclusion exists, so it is not dropped as unnecessary.

    Excluding a file from the spelling gate is only defensible while the file is
    an untouched third-party artifact. The digests in the README are the record
    of that claim, so a silent edit to a vendored schema -- including one made
    just to silence the spellchecker -- fails here.
    """
    recorded = _readme_digests()
    schemas = sorted(FIXTURE_DIR.glob("*.schema.json"))

    assert schemas, f"no vendored schemas found under {FIXTURE_DIR}"
    for path in schemas:
        assert path.name in recorded, f"{path.name} has no digest recorded in {README.name}"
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        assert actual == recorded[path.name], f"{path.name} differs from the digest this repository vendored"
