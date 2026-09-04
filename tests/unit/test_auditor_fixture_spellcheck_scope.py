"""Spellcheck scope around the recorded auditor conformance fixture (issue #5057).

The exported bundle carries an Ed25519 JWK and detached signatures over the
recorded run. Those are base64url runs, and the spellchecker reads three-letter
slices of them as English words: a slice of the public key's ``x`` coordinate
was read as a misspelling of "Had"/"And", which failed the spelling gate and,
through it, the whole CI gate.

The bytes cannot be corrected. Changing one character of the key invalidates
the receipt that ``tests/conformance/auditor/test_vectors.py`` verifies offline,
and the whole tree is machine-made anyway -- ``scripts/auditor_conformance.py
regenerate`` rewrites it by re-running the scenario, so a spelling fix here
would be reverted by the next regeneration. So the fixture is excluded.

An exclusion that grew to cover the human-authored README and harness beside it
would silently stop spellchecking prose, which is the second thing this file
guards: the fixture stays out of scope, the prose stays in.
"""

from __future__ import annotations

import re
import tomllib
from fnmatch import fnmatch
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG = REPO_ROOT / "typos.toml"
FIXTURE_DIR = REPO_ROOT / "tests" / "conformance" / "auditor" / "fixture"
README = REPO_ROOT / "tests" / "conformance" / "auditor" / "README.md"
RECORDER = REPO_ROOT / "tests" / "conformance" / "auditor" / "recorder.py"

#: A run this long of base64url alphabet is not a word in any language the
#: scanner knows, and every three-letter slice of it is a coin flip against
#: the dictionary.
_OPAQUE_RUN = re.compile(r"[A-Za-z0-9_/+=-]{40,}")


def _exclude_patterns() -> list[str]:
    data = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
    files = data.get("files", {})
    patterns = files.get("extend-exclude", [])
    assert isinstance(patterns, list), "files.extend-exclude must be a list"
    return [str(p) for p in patterns]


def _is_excluded(path: Path) -> bool:
    rel = path.relative_to(REPO_ROOT).as_posix()
    return any(fnmatch(rel, pattern) for pattern in _exclude_patterns())


def test_recorded_fixture_is_excluded_from_the_spellcheck() -> None:
    """Every committed fixture file is signature/key content, not prose."""
    generated = sorted(p for p in FIXTURE_DIR.rglob("*") if p.is_file())

    assert generated, f"no fixture files found under {FIXTURE_DIR}"
    for path in generated:
        assert _is_excluded(path), f"{path.name} would be spellchecked; its signed bytes cannot be edited"


def test_the_harness_prose_is_still_spellchecked() -> None:
    """The exclusion must not widen onto the human-authored harness."""
    for path in (README, RECORDER):
        assert path.is_file(), f"{path.name} is missing"
        assert not _is_excluded(path), f"{path.name} is prose-bearing and must stay in the spellcheck scope"


def test_each_fixture_file_carries_content_no_spellchecker_can_read() -> None:
    """Pin why the exclusion exists, so it is not dropped as unnecessary.

    Asserting the one token that happened to break the gate would prove
    nothing after the next regeneration mints different bytes. The stable
    property is that every fixture file is opaque at all -- either binary,
    or carrying a base64url run -- which is what makes a dictionary
    collision a matter of time rather than an accident.
    """
    for path in sorted(p for p in FIXTURE_DIR.rglob("*") if p.is_file()):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # binary payload: no dictionary can read it either

        assert _OPAQUE_RUN.search(text), f"{path.name} carries no opaque run; is it still a recorded artefact?"
