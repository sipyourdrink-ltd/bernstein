"""Property tests for the orchestrator Write-Ahead Log.

Two families of properties:

- **Append-then-verify** - random sequences of decisions must produce
  a hash chain that ``WALReader.verify_chain()`` accepts.
- **Tamper detection** - any single-byte mutation of an on-disk WAL
  line must break the chain (``verify_chain`` reports at least one
  error).

State-machine coverage (Hypothesis ``RuleBasedStateMachine``) is in a
companion file (``test_wal_recovery_machine.py``) so this file stays
tightly focused on the chain primitives.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from bernstein.core.persistence.wal import WALReader, WALWriter

# ---------------------------------------------------------------------------
# Byte-coverage contract for the WAL hash chain
# ---------------------------------------------------------------------------
#
# ``verify_chain`` recomputes each entry's digest from the *parsed* JSON
# payload, so the chain guarantees value-level integrity of every hashed
# field - not byte-level integrity of the file's textual encoding. An
# exhaustive single-bit flip over every byte position of a generated WAL
# leaves exactly two classes of byte undisturbed:
#
# 1. **The file's final newline.** It is a record separator, not part of
#    any entry payload, and ``verify_chain`` strips it before parsing.
#    Interior newlines *are* covered: flipping one (0x0A -> 0x0B) merges
#    two records into a single unparseable line.
# 2. **Sub-ULP digits of ``timestamp``.** ``repr()`` of a ``time.time()``
#    value needs 17 significant digits often enough to matter (~27% of
#    samples). The 17th digit steps by 1e-7 s while the ULP of a double
#    near 1.7e9 is 2.4e-7 s, so a +/-1 change there can parse back to the
#    identical float and re-serialise to identical canonical bytes.
#
# Class (2) is why this test was flaky: whether an aliasing digit existed
# at all depended on the wall-clock value at write time, so the same
# ``flip_offset`` was detectable on one machine and undetectable on
# another. Freezing the clock to a value with a short exact ``repr``
# removes the aliasing digit entirely, leaving class (1) as the only
# exclusion - one deterministic position, the last byte of the file.
#
# Every other byte is covered and asserted on: ``seq``, ``prev_hash``,
# ``entry_hash``, ``decision_type``, ``inputs``, ``output``, ``actor``,
# ``committed``, and all structural punctuation.

# A wall-clock value whose repr is short and exact, so that every digit of
# the serialised timestamp is hash-covered (no sub-ULP aliasing digit).
_FROZEN_TS = 1_700_000_000.5

_ALPHABET = st.characters(min_codepoint=0x20, max_codepoint=0x7E)
_TEXT = st.text(_ALPHABET, min_size=1, max_size=24)
_PAYLOAD = st.dictionaries(
    keys=st.text(_ALPHABET, min_size=1, max_size=8),
    values=st.one_of(_TEXT, st.integers(-1_000, 1_000), st.booleans()),
    max_size=4,
)


def _writer_for(run_id: str) -> tuple[WALWriter, Path]:
    """Return a writer with its own ``.sdd/`` tempdir."""
    sdd = Path(tempfile.mkdtemp(prefix="bernstein-prop-wal-"))
    return WALWriter(run_id=run_id, sdd_dir=sdd), sdd


@given(
    decisions=st.lists(
        st.tuples(_TEXT, _PAYLOAD, _PAYLOAD, _TEXT, st.booleans()),
        min_size=1,
        max_size=12,
    ),
)
def test_chain_extends_with_arbitrary_decisions(
    decisions: list[tuple[str, dict[str, Any], dict[str, Any], str, bool]],
) -> None:
    """Random ``append()`` sequences must verify cleanly."""
    writer, sdd = _writer_for("run-prop-1")
    for decision_type, inputs, output, actor, committed in decisions:
        writer.append(
            decision_type=decision_type,
            inputs=inputs,
            output=output,
            actor=actor,
            committed=committed,
        )

    valid, errors = WALReader("run-prop-1", sdd).verify_chain()
    assert valid, f"chain rejected its own writes: {errors}"


@settings(max_examples=30)
@given(
    decisions=st.lists(
        st.tuples(_TEXT, _PAYLOAD, _PAYLOAD, _TEXT, st.booleans()),
        min_size=2,
        max_size=8,
    ),
    flip_offset=st.integers(min_value=0, max_value=20_000),
)
def test_single_byte_flip_breaks_verify_chain(
    decisions: list[tuple[str, dict[str, Any], dict[str, Any], str, bool]],
    flip_offset: int,
) -> None:
    """Flipping any hash-covered byte of the on-disk WAL must trip ``verify_chain``.

    The flip range spans every byte of the file except the trailing record
    separator, and the clock is frozen so no sub-ULP ``timestamp`` digit can
    alias. See the byte-coverage contract at the top of this module for why
    those are the only two exclusions.
    """
    writer, sdd = _writer_for("run-prop-flip")
    with mock.patch("bernstein.core.persistence.wal.time.time", return_value=_FROZEN_TS):
        for decision_type, inputs, output, actor, committed in decisions:
            writer.append(
                decision_type=decision_type,
                inputs=inputs,
                output=output,
                actor=actor,
                committed=committed,
            )

    wal_path = sdd / "runtime" / "wal" / "run-prop-flip.wal.jsonl"
    raw = wal_path.read_bytes()
    if not raw:
        pytest.skip("WAL produced no bytes")

    # The final newline separates records; it is outside every entry payload
    # and therefore outside the digest. Every preceding byte is covered.
    assert raw.endswith(b"\n"), "WAL records are newline-terminated"
    covered = len(raw) - 1
    if covered == 0:
        pytest.skip("WAL holds nothing but a separator")

    pos = flip_offset % covered
    mutated = bytearray(raw)
    mutated[pos] ^= 0x01
    wal_path.write_bytes(bytes(mutated))

    reader = WALReader("run-prop-flip", sdd)
    valid, errors = reader.verify_chain()
    context = raw[max(0, pos - 30) : pos + 30]
    assert not valid, (
        f"WAL byte flip went undetected at offset {pos}/{covered} "
        f"(0x{raw[pos]:02x} -> 0x{mutated[pos]:02x}), context: {context!r}"
    )
    assert errors


@given(
    n=st.integers(min_value=2, max_value=10),
    actor=_TEXT,
)
def test_seq_is_strictly_monotonic(n: int, actor: str) -> None:
    """Sequence numbers must increment by exactly one per append."""
    writer, _ = _writer_for("run-seq")
    seqs: list[int] = []
    for i in range(n):
        entry = writer.append(
            decision_type="t",
            inputs={"i": i},
            output={"r": i},
            actor=actor,
        )
        seqs.append(entry.seq)

    assert seqs == list(range(seqs[0], seqs[0] + n))
