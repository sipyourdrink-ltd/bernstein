"""Undecodable bytes are a discarded physical line, not an exception (#3971).

Every fixture here is built from RAW BYTES rather than from text. A journal
written through ``str`` cannot hold an incomplete multi-byte character at all -
the encode would either succeed or raise before anything reached the disk - so a
text fixture would exercise the JSON parser and never the decode path this
module is about.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.replay.journal import (
    JournalParseError,
    load_events,
)

# A row shaped like the ones ``EventJournal`` appends. Held as bytes because the
# point of every fixture below is what is on the disk, not what a str would say.
_GOOD = tuple(
    json.dumps(
        {
            "event": "step",
            "index": index,
            "prev_hash": "",
            "payload_hash": "p" * 8,
            "event_hash": f"h{index}" * 4,
        }
    ).encode("utf-8")
    for index in range(3)
)

# ``"café"`` with the two-byte ``é`` cut after its lead byte: exactly what a
# crash partway through a write leaves when it stops inside a character.
_TORN_MID_CHARACTER = json.dumps(
    {"event": "step", "index": 3, "prev_hash": "", "note": "café"},
    ensure_ascii=False,
).encode("utf-8")[:-3]

# The SAME crash, stopped between two characters instead of inside one. This is
# the control: on a tree without the fix these two files behave differently, and
# the contract says they must not.
_TORN_BETWEEN_CHARACTERS = _GOOD[0][:40]


def _write(path: Path, *chunks: bytes, sep: bytes = b"\n") -> Path:
    path.write_bytes(sep.join(chunks))
    return path


def test_tolerant_reader_discards_a_tail_torn_mid_character(tmp_path: Path) -> None:
    """The documented contract: a partial trailing write cannot wedge a reader."""
    path = _write(tmp_path / "journal.jsonl", *_GOOD, _TORN_MID_CHARACTER)

    loaded = load_events(path)

    assert loaded.discarded_line_indices == (3,)
    assert [row["event_hash"] for row in loaded.events] == ["h0" * 4, "h1" * 4, "h2" * 4]


def test_strict_reader_raises_journal_parse_error_naming_the_physical_line(
    tmp_path: Path,
) -> None:
    """A diagnostic caller gets the same error type and the same index it gets
    for unparsable JSON - not a bare ``UnicodeDecodeError`` from the codec."""
    path = _write(tmp_path / "journal.jsonl", *_GOOD, _TORN_MID_CHARACTER)

    with pytest.raises(JournalParseError) as excinfo:
        load_events(path, strict=True)

    assert "physical line 3" in str(excinfo.value)


@pytest.mark.parametrize(
    ("label", "tail"),
    [
        ("between characters", _TORN_BETWEEN_CHARACTERS),
        ("mid character", _TORN_MID_CHARACTER),
    ],
)
def test_both_tear_positions_produce_the_same_outcome(tmp_path: Path, label: str, tail: bytes) -> None:
    """THE POINT OF THE ISSUE, ASSERTED DIRECTLY.

    One class of crash, two places it can stop, and before the fix the reader
    reported a discarded line for one and raised out of the codec for the other.
    Parametrised rather than written twice so the two cases cannot drift.
    """
    path = _write(tmp_path / f"{label.replace(' ', '-')}.jsonl", *_GOOD, tail)

    loaded = load_events(path)

    assert loaded.discarded_line_indices == (3,)
    assert len(loaded.events) == 3


def test_undecodable_bytes_in_the_middle_do_not_hide_later_rows(
    tmp_path: Path,
) -> None:
    """A torn tail is the motivating case, but the reader must not stop early on
    a bad line anywhere: the strict reader's whole job is to see every physical
    line, and the tolerant one's is to keep going."""
    path = _write(tmp_path / "journal.jsonl", _GOOD[0], b"\xff\xfe not utf-8", _GOOD[1])

    loaded = load_events(path)

    assert loaded.discarded_line_indices == (1,)
    assert [row["event_hash"] for row in loaded.events] == ["h0" * 4, "h1" * 4]


def test_physical_line_indices_stay_physical_across_universal_newlines(
    tmp_path: Path,
) -> None:
    """The indices are DEFINED by universal-newline splitting, and this pins it.

    The rows here are separated by lone carriage returns, which end a line in
    text mode and do not in ``bytes.split(b"\\n")``. An implementation that
    reads bytes and splits on ``\\n`` sees this whole file as physical line 0
    and would report ``(0,)``; text-mode numbering puts the bad row at 2.
    """
    path = _write(
        tmp_path / "journal.jsonl",
        _GOOD[0],
        _GOOD[1],
        b"\xc3",
        _GOOD[2],
        sep=b"\r",
    )

    loaded = load_events(path)

    assert loaded.discarded_line_indices == (2,)
    assert len(loaded.events) == 3


def test_a_bad_byte_inside_otherwise_valid_json_is_discarded_not_repaired(
    tmp_path: Path,
) -> None:
    """THE TEST THAT PINS THE ERROR POLICY, AND THE REASON IT IS NOT ``replace``.

    This row is syntactically perfect JSON whose only defect is one byte that is
    not UTF-8, sitting inside a string. A reader that decoded with
    ``errors="replace"`` would turn that byte into U+FFFD, parse the row, and
    hand the caller a *silently altered* event whose ``payload_hash`` no longer
    describes its own payload - a corrupted row admitted to the chain rather
    than a discarded physical line. The tolerant reader must refuse it, and the
    strict reader must say which line it was.
    """
    row = (
        b'{"event": "step", "index": 0, "prev_hash": "", "payload_hash": "pppppppp", '
        b'"event_hash": "h0h0h0h0", "note": "a\xffb"}'
    )
    json.loads(row.decode("utf-8", "replace"))  # syntactically valid, only the byte is bad
    path = _write(tmp_path / "journal.jsonl", row)

    loaded = load_events(path)

    assert loaded.events == []
    assert loaded.discarded_line_indices == (0,)

    with pytest.raises(JournalParseError, match="physical line 0"):
        load_events(path, strict=True)


def test_a_valid_non_ascii_row_is_not_discarded(tmp_path: Path) -> None:
    """The fix widens what counts as malformed, so it has to be pinned that it
    did not widen it to "anything above U+007F"."""
    row = json.dumps(
        {"event": "step", "index": 0, "prev_hash": "", "note": "café ☃ 🜁"},
        ensure_ascii=False,
    ).encode("utf-8")
    path = _write(tmp_path / "journal.jsonl", row)

    loaded = load_events(path)

    assert loaded.discarded_line_indices == ()
    assert loaded.events[0]["note"] == "café ☃ 🜁"


def test_an_escaped_lone_surrogate_is_still_a_json_question_not_a_decode_one(
    tmp_path: Path,
) -> None:
    """UNCHANGED BEHAVIOUR, PINNED BECAUSE THE FIX COULD PLAUSIBLY HAVE MOVED IT.

    ``"\\udcff"`` written as a JSON escape is pure ASCII on disk: the bytes are
    valid UTF-8 and only the decoded *value* holds a lone surrogate. The decode
    policy has no opinion about that, and this row loaded before the fix and
    still does. A ``_is_decodable`` check applied to the parsed value instead of
    to the physical line would start discarding it.
    """
    row = rb'{"event": "step", "index": 0, "prev_hash": "", "note": "\udcff"}'
    assert row.isascii()
    path = _write(tmp_path / "journal.jsonl", row)

    loaded = load_events(path)

    assert loaded.discarded_line_indices == ()
    assert loaded.events[0]["note"] == "\udcff"
