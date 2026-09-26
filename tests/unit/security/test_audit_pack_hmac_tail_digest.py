"""Regression coverage for ``_hmac_chain_tail_digest``'s JSONL parsing (#5950).

The tail digest scans an audit-chain JSONL file for the last entry carrying
an ``hmac`` field, quoting it in the evidence pack as a single content hash
for the whole chain. Every test exercising that scan so far has written one
well-formed ``{"hmac": ...}`` dict per line -- a non-dict JSON value
(a bare string, an array, a number, ``null``, a boolean) never reached the
``isinstance(entry, dict) and "hmac" in entry`` guard under test. Losing that
guard -- narrowing it from ``and`` to ``or``, say -- would abort the pack
export with a ``TypeError`` or ``KeyError`` on the first such line: every
junk shape a bare non-dict value can take either fails the membership check
(``"hmac" in 42``) or reaches ``entry["hmac"]`` on something that cannot be
subscripted by a string key, so this crashes loudly rather than quoting the
wrong tail. Nothing here would have noticed either way before this file
existed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bernstein.core.security.audit_pack import _hmac_chain_tail_digest

if TYPE_CHECKING:
    from pathlib import Path


def _write_chain(audit_dir: Path, lines: list[str]) -> None:
    audit_dir.mkdir(parents=True, exist_ok=True)
    (audit_dir / "chain.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_non_dict_and_malformed_lines_do_not_break_tail_selection(tmp_path: Path) -> None:
    """The load-bearing case: a mix of JSON types and two malformed lines,
    with a valid dict-with-hmac entry on either side.

    Every JSON type ``isinstance(entry, dict)`` exists to exclude is present
    here, plus two lines that fail to parse: one that looks like it should
    (a JSON value where a string is expected) and one truncated mid-object --
    the shape a process that died mid-append actually leaves behind, and the
    real-world case the ``continue`` exists for. The guard must skip all of
    them without raising, neither malformed line may stop the scan, and the
    later valid dict must still win over the earlier one.
    """
    audit_dir = tmp_path / "audit"
    _write_chain(
        audit_dir,
        [
            '{"hmac":"aaaa"}',
            '"hmac"',
            '["hmac"]',
            "42",
            "null",
            "true",
            '{"hmac": not valid json}',
            '{"hmac": "truncated by a crash mid-write"',
            '{"hmac":"bbbb"}',
        ],
    )

    ref, _mtime, details = _hmac_chain_tail_digest(audit_dir)

    assert ref == "sha256:bbbb"
    assert details["lines"] == 9


def test_all_non_dict_values_report_no_hmac_found(tmp_path: Path) -> None:
    """No dict ever reaches the guard -- every line is counted, none matches."""
    audit_dir = tmp_path / "audit"
    _write_chain(audit_dir, ['"hmac"', '["hmac"]', "42"])

    ref, _mtime, details = _hmac_chain_tail_digest(audit_dir)

    assert ref == ""
    assert details["reason"] == "no hmac field found"
    assert details["lines"] == 3


def test_missing_audit_dir_returns_empty(tmp_path: Path) -> None:
    ref, mtime, details = _hmac_chain_tail_digest(tmp_path / "audit")

    assert (ref, mtime, details) == ("", 0.0, {})


def test_audit_dir_with_no_jsonl_files_reports_reason(tmp_path: Path) -> None:
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()

    ref, _mtime, details = _hmac_chain_tail_digest(audit_dir)

    assert ref == ""
    assert details["reason"] == "no audit log files present"
