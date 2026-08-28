"""Spellcheck scope around the committed trust-record vectors (issue #4692).

The vectors carry real Ed25519 signatures and a `did:key` multibase subject.
Those are base64url and base58 runs, and the spellchecker reads two-letter
slices of them as English words: one slice of the delegated child's signature
was read as a misspelling, which failed the spelling gate and, through it, the
whole CI gate.

The bytes cannot be corrected: changing one character of a signature
invalidates it, and `tests/unit/test_trust_record_format_vectors.py` verifies
those signatures offline. So the generated files are excluded from the scan.

An exclusion that grew to cover the human-authored builder script beside them
would silently stop spellchecking prose, which is the second thing this file
guards: the vectors stay out of scope, the builder stays in.
"""

from __future__ import annotations

import json
import re
import tomllib
from fnmatch import fnmatch
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG = REPO_ROOT / "typos.toml"
VECTOR_DIR = REPO_ROOT / "tests" / "fixtures" / "trust-record-vectors"
BUILDER = VECTOR_DIR / "_build_trust_record_vectors.py"

#: A run this long of signature/multibase alphabet is not a word in any
#: language the scanner knows, and every two-letter slice of it is a coin
#: flip against the dictionary.
_OPAQUE_RUN = re.compile(r"[A-Za-z0-9_-]{40,}")


def _exclude_patterns() -> list[str]:
    data = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
    files = data.get("files", {})
    patterns = files.get("extend-exclude", [])
    assert isinstance(patterns, list), "files.extend-exclude must be a list"
    return [str(p) for p in patterns]


def _is_excluded(path: Path) -> bool:
    rel = path.relative_to(REPO_ROOT).as_posix()
    return any(fnmatch(rel, pattern) for pattern in _exclude_patterns())


def test_generated_vectors_are_excluded_from_the_spellcheck() -> None:
    """Every committed vector is signature/multibase content, not prose."""
    generated = sorted(p for p in VECTOR_DIR.iterdir() if p.suffix in {".json", ".pem"})

    assert generated, f"no generated vectors found under {VECTOR_DIR}"
    for path in generated:
        assert _is_excluded(path), f"{path.name} would be spellchecked; its signature bytes cannot be edited"


def test_the_vector_builder_is_still_spellchecked() -> None:
    """The exclusion must not widen onto the human-authored script."""
    assert BUILDER.is_file(), "the vector builder is missing"
    assert not _is_excluded(BUILDER), "the builder is prose-bearing and must stay in the spellcheck scope"


def test_each_vector_carries_content_no_spellchecker_can_read() -> None:
    """Pin why the exclusion exists, so it is not dropped as unnecessary.

    Asserting the one token that happened to break the gate would prove
    nothing after the next regeneration mints different bytes. The stable
    property is that every vector contains an opaque signature run at all --
    that is what makes a dictionary collision a matter of time rather than
    an accident.
    """
    for path in sorted(VECTOR_DIR.glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        signature = json.dumps(record.get("signature", {}))

        assert _OPAQUE_RUN.search(signature), f"{path.name} carries no signature run; is it still a signed vector?"
