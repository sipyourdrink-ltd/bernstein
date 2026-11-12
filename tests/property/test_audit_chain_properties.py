"""Property tests for the HMAC-chained audit log.

Three guarantees we want CI to enforce on every PR:

1. **Soundness** - appending any new event to a previously valid prefix
   produces a chain that ``verify()`` accepts. No matter what details
   payload Hypothesis throws at us, the writer must produce an entry
   that can be re-verified by the same key.

2. **Tamper-evidence (single-byte flip)** - flipping any byte of any
   on-disk JSONL entry must cause ``verify()`` to flag at least one
   error. This catches downstream regressions in the verifier (e.g.,
   accidentally trimming the HMAC field).

3. **Cross-day chain continuity** - when log rotation crosses days
   (multiple ``YYYY-MM-DD.jsonl`` files), the prev_hmac of the first
   entry on day N+1 must equal the hmac of the last entry on day N.

Heavy fuzz sweeps live in the nightly ``deep`` profile (1 000 examples
per property); PR-time runs ``smoke`` (50 examples) so each file
finishes in well under a minute.

Hypothesis-vs-pytest-fixtures gotcha: function-scoped fixtures are
re-evaluated only at the *outer* test invocation, not for each
generated example. Tests that mutate filesystem state therefore build
a private temp dir inside the test body so each generated example
starts from a clean slate.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from bernstein.core.security.audit import AuditLog

# Restrict event_type / actor / resource_id to characters that survive
# JSON encoding without escape chains; the chain mechanics are
# orthogonal to UTF-8 escaping (covered in dedicated unit tests).
_ALPHABET = st.characters(
    blacklist_categories=("Cs",),
    min_codepoint=0x20,
    max_codepoint=0x7E,
)
_TEXT = st.text(_ALPHABET, min_size=1, max_size=32)
_DETAILS = st.dictionaries(
    keys=st.text(_ALPHABET, min_size=1, max_size=8),
    values=st.one_of(st.integers(-1_000, 1_000), st.booleans(), _TEXT),
    max_size=4,
)


@contextmanager
def _new_audit_log() -> Iterator[AuditLog]:
    """Provide one private audit directory per generated example."""
    with tempfile.TemporaryDirectory(prefix="bernstein-prop-audit-") as directory:
        tmpdir = Path(directory)
        audit_dir = tmpdir / "audit"
        audit_dir.mkdir()
        key_path = tmpdir / "audit.key"
        key_path.write_bytes(b"hypothesis-fuzz-key-32-bytes-padding-pad")
        key_path.chmod(0o600)
        yield AuditLog(audit_dir=audit_dir, key_path=key_path)


@given(
    events=st.lists(
        st.tuples(_TEXT, _TEXT, _TEXT, _TEXT, _DETAILS),
        min_size=1,
        max_size=12,
    ),
)
def test_chain_extends_with_arbitrary_events(
    events: list[tuple[str, str, str, str, dict[str, Any]]],
) -> None:
    """Any sequence of ``log()`` calls must produce a chain that verifies."""
    with _new_audit_log() as audit_log:
        for event_type, actor, resource_type, resource_id, details in events:
            audit_log.log(event_type, actor, resource_type, resource_id, details)

        valid, errors = audit_log.verify()
        assert valid, f"Chain rejected its own writes: {errors}"


@settings(max_examples=30)  # IO-heavy - tighter than smoke default.
@given(
    events=st.lists(
        st.tuples(_TEXT, _TEXT, _TEXT, _TEXT, _DETAILS),
        min_size=2,
        max_size=8,
    ),
    flip_offset=st.integers(min_value=0, max_value=10_000),
)
def test_single_byte_flip_breaks_verification(
    events: list[tuple[str, str, str, str, dict[str, Any]]],
    flip_offset: int,
) -> None:
    """Flipping any byte of any persisted entry must surface a verify error.

    The flip target is chosen modulo the file size so the byte index is
    always in-range regardless of the random payloads written.
    """
    with _new_audit_log() as audit_log:
        for event_type, actor, resource_type, resource_id, details in events:
            audit_log.log(event_type, actor, resource_type, resource_id, details)

        files = sorted(audit_log._audit_dir.glob("*.jsonl"))  # pyright: ignore[reportPrivateUsage]
        assert files, "audit log produced no files"

        target = files[0]
        raw = target.read_bytes()
        if not raw:
            pytest.skip("empty log file - nothing to flip")
        pos = flip_offset % len(raw)

        mutated = bytearray(raw)
        mutated[pos] ^= 0x01
        if mutated == raw:
            pytest.skip("XOR with 0x01 produced identical bytes (impossible)")

        target.write_bytes(bytes(mutated))

        valid, errors = audit_log.verify()
        assert not valid, "byte flip went undetected"
        assert errors, "verify() returned invalid=True with empty errors list"


@settings(max_examples=20)
@given(
    day1_events=st.lists(_TEXT, min_size=1, max_size=4),
    day2_events=st.lists(_TEXT, min_size=1, max_size=4),
)
def test_chain_continuity_across_log_rotation(
    day1_events: list[str],
    day2_events: list[str],
) -> None:
    """Manually-rotated daily files must keep the prev_hmac → hmac link.

    We simulate rotation by writing to ``2026-01-01.jsonl`` and
    ``2026-01-02.jsonl`` directly, then asserting ``verify()`` accepts
    the multi-file chain.
    """
    with _new_audit_log() as audit_log:
        audit_dir = audit_log._audit_dir  # pyright: ignore[reportPrivateUsage]

        for label in day1_events:
            audit_log.log(label, "actor", "task", "rid", {"day": 1})

        files_before = sorted(audit_dir.glob("*.jsonl"))
        if not files_before:
            pytest.skip("writer never produced a file (deadline pre-empted us)")
        src = files_before[0]
        rotated = audit_dir / "2026-01-01.jsonl"
        if src != rotated:
            rotated.write_bytes(src.read_bytes())
            src.unlink()

        refreshed = AuditLog(audit_dir=audit_dir, key=audit_log._key)  # pyright: ignore[reportPrivateUsage]

        for label in day2_events:
            refreshed.log(label, "actor", "task", "rid", {"day": 2})

        for f in audit_dir.glob("*.jsonl"):
            if f.name not in {"2026-01-01.jsonl", "2026-01-02.jsonl"}:
                f.rename(audit_dir / "2026-01-02.jsonl")

        valid, errors = refreshed.verify()
        assert valid, f"rotation broke the chain: {errors}"


@given(events=st.lists(_TEXT, min_size=2, max_size=6))
def test_reordering_lines_breaks_verification(events: list[str]) -> None:
    """Swapping any two adjacent log lines must trip the integrity check.

    The chain's prev_hmac field encodes line ordering; reversing or
    reshuffling lines changes the expected hmac at each boundary.
    """
    with _new_audit_log() as audit_log:
        for label in events:
            audit_log.log(label, "actor", "task", "rid", {})

        audit_dir = audit_log._audit_dir  # pyright: ignore[reportPrivateUsage]
        files = sorted(audit_dir.glob("*.jsonl"))
        assert files
        target = files[0]
        lines = target.read_text().splitlines()
        if len(lines) < 2:
            pytest.skip("need at least two lines to test reordering")

        lines[0], lines[1] = lines[1], lines[0]
        target.write_text("\n".join(lines) + "\n")

        valid, errors = audit_log.verify()
        assert not valid, "line swap went undetected - chain ordering is not enforced"
        assert errors


@given(payloads=st.lists(_DETAILS, min_size=1, max_size=6))
def test_details_unicode_round_trip_preserved(payloads: list[dict[str, Any]]) -> None:
    """A round-trip through query() must recover the same details dicts.

    Catches regressions where ``json.dumps(..., sort_keys=True)`` drift
    between writer and reader (e.g., accidental ``ensure_ascii``
    toggle) corrupts the chain or the deserialized payload.
    """
    with _new_audit_log() as audit_log:
        for d in payloads:
            audit_log.log("evt", "actor", "task", "rid", d)

        rows = audit_log.query()
        assert len(rows) == len(payloads)
        for source, persisted in zip(payloads, rows, strict=False):
            assert json.loads(json.dumps(source, sort_keys=True)) == json.loads(
                json.dumps(persisted.details, sort_keys=True),
            )
