"""Undecodable bytes are a discarded physical line, not an exception (#3971, #4016).

Every fixture here is built from RAW BYTES rather than from text. A journal
written through ``str`` cannot hold an incomplete multi-byte character at all -
the encode would either succeed or raise before anything reached the disk - so a
text fixture would exercise the JSON parser and never the decode path this
module is about.

#3971 covers ``load_events``; #4016 covers ``EventJournal.event_count``, the
sibling reader of the same file, in the final section. They share this module
because they share the fixtures: the two readers agreeing about one file *is*
the contract, and splitting them would let the fixtures drift apart.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.replay.journal import (
    EventJournal,
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


# ---------------------------------------------------------------------------
# EventJournal.event_count (#4016) - the sibling reader of load_events.
#
# Same file, same crash, and until this landed a different decode policy. The
# fixtures stay raw bytes for the reason in the module docstring above.
# ---------------------------------------------------------------------------

# A row that is valid UTF-8 and unparsable JSON: the tear stopped after a
# newline-free prefix rather than inside a character. It is the control that
# separates the two policies, because no decode question arises for it at all
# and the old counter still disagreed with ``load_events`` by one.
_UNPARSABLE_JSON = _GOOD[0][:40].replace(b"\n", b"")

# A row that decodes and parses and is still not an event.
_NON_OBJECT_ROW = b'"just a string"'


def _journal_over(sdd_dir: Path, *chunks: bytes) -> EventJournal:
    """An ``EventJournal`` whose file already holds ``chunks`` on disk.

    The plain constructor rather than ``resume`` on purpose: ``resume`` calls
    ``load_events`` and refuses any file with a discarded line, so it can never
    reach ``event_count`` on the fixtures below. The reachable callers are the
    ones handed an injected journal - ``activity.record_activity_result`` and
    ``subagent_delegation`` - and that journal is the orchestrator's plainly
    constructed ``_recorder``.
    """
    journal = EventJournal(run_id="run-count", sdd_dir=sdd_dir)
    _write(journal.path, *chunks)
    return journal


def test_event_count_survives_a_tail_torn_mid_character(tmp_path: Path) -> None:
    """THE ISSUE, ASSERTED DIRECTLY.

    ``UnicodeDecodeError`` derives from ``ValueError``, so the method's own
    ``except OSError`` never caught it and it propagated out of a counter
    documented to answer ``0`` when it cannot read.
    """
    journal = _journal_over(tmp_path, *_GOOD, _TORN_MID_CHARACTER)

    assert journal.event_count() == 3


@pytest.mark.parametrize(
    ("label", "tail"),
    [
        ("clean", None),
        ("torn mid character", _TORN_MID_CHARACTER),
        ("torn between characters", _TORN_BETWEEN_CHARACTERS),
        ("valid utf-8, unparsable json", _UNPARSABLE_JSON),
        ("non-object row", _NON_OBJECT_ROW),
    ],
)
def test_event_count_is_len_load_events(tmp_path: Path, label: str, tail: bytes | None) -> None:
    """THE RELATIONSHIP THE ISSUE ASKED TO HAVE ASSERTED RATHER THAN DESCRIBED.

    ``event_count() == len(load_events(path).events)``, for every shape of bad
    row and for a clean journal. The ``clean`` case is the control: it passes
    on either policy, so a run where only it is green is a run where these
    fixtures stopped reaching the counter.

    The last two cases carry no decode question at all - they are pure ASCII -
    and the old counter still disagreed by one on both. Fixing only the decode
    would have left them, which is the third policy the issue warns about.
    """
    chunks = (*_GOOD, tail) if tail is not None else _GOOD
    journal = _journal_over(tmp_path / label.replace(" ", "-").replace(",", ""), *chunks)

    assert journal.event_count() == len(load_events(journal.path).events)


def test_event_count_is_the_index_resume_would_continue_from(tmp_path: Path) -> None:
    """Why *skip* and not *count*, tied to the writer rather than to taste.

    ``resume`` sets its next index to ``len(events)``, so the row appended
    after a malformed one carries that number - not a line number. A counter
    that disagreed would make ``event_count() - 1``, which is how every caller
    spells "the row I just wrote", name a row that is not in the journal.
    """
    chained = EventJournal(run_id="run-count", sdd_dir=tmp_path)
    for index in range(3):
        chained.record(f"step-{index}")

    resumed = EventJournal.resume(run_id="run-count", sdd_dir=tmp_path)
    resumed.record("appended-after-resume")
    appended = load_events(resumed.path).events[-1]

    assert resumed.event_count() - 1 == appended["index"]


def test_event_count_still_answers_zero_when_the_file_cannot_be_read(
    tmp_path: Path,
) -> None:
    """The ``except OSError`` is now exactly what it says.

    No decode error can reach it any more, so the handler's remaining job is
    the one it is named for. A directory standing where the journal file
    belongs raises ``IsADirectoryError`` - an ``OSError`` - from the open.
    """
    journal = EventJournal(run_id="run-count", sdd_dir=tmp_path)
    journal.path.mkdir(parents=True, exist_ok=True)

    assert journal.event_count() == 0


def test_event_count_does_not_swallow_a_non_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The handler stays NARROW, and that is the whole shape of this bug.

    The defect was a ``ValueError`` subclass reaching a caller as an exception
    because ``except OSError`` did not catch it. Widening the handler to
    ``Exception`` would have "fixed" that by turning any future reader failure
    into a silent ``0`` - a wrong count is worse than a raised one here,
    because ``event_count() - 1`` would then stamp ``-1`` into a receipt.
    Nothing about the scan being shared prevents that widening, so it is
    pinned: a non-``OSError`` out of the reader propagates.

    The ``invalidate_count()`` call is load-bearing and was added by #4026,
    which gave ``event_count`` a cache. Without it this test still passed
    while exercising nothing: ``record`` primes the cache, so the patched
    reader was never reached and the ``pytest.raises`` below was satisfied
    by no call at all. Forcing the scan is what keeps the handler's width
    under test rather than its reachability.
    """
    journal = EventJournal(run_id="run-count", sdd_dir=tmp_path)
    journal.record("one")

    def _boom(*_args: object, **_kwargs: object) -> object:
        raise ValueError("reader failed for some reason that is not I/O")

    monkeypatch.setattr("bernstein.core.replay.journal.load_events", _boom)
    journal.invalidate_count()

    with pytest.raises(ValueError, match="not I/O"):
        journal.event_count()
