"""Tests for the audit chain-tail resolver's tolerance of junk log lines.

`_hmac_chain_tail_digest` reads whatever is on disk in the newest audit
`.jsonl`. JSON Lines guarantees one JSON *value* per line, not one object, so a
bare string, array, number, `null` or `true` is a well-formed line the parser
accepts and hands back as a non-dict. The `isinstance(entry, dict)` guard is
the only thing between those values and `"hmac" in entry` -- which is a
substring test on a string, a membership test on a list, and a `TypeError` on a
number. This file pins that guard: see #5950.
"""

from __future__ import annotations

from pathlib import Path

from bernstein.core.security import audit_pack


def _write_log(audit_dir: Path, name: str, lines: list[str]) -> Path:
    audit_dir.mkdir(parents=True, exist_ok=True)
    path = audit_dir / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_tail_digest_skips_non_dict_json_lines(tmp_path: Path) -> None:
    """Junk lines are skipped; the tail is the last *dict* entry carrying an hmac.

    Every non-dict line here is one the guard has to reject for a different
    reason: `"hmac"` contains the key as a substring, `["hmac"]` contains it as
    a member, and `42` / `null` / `true` raise `TypeError` on `in`.
    """
    audit_dir = tmp_path / "audit"
    _write_log(
        audit_dir,
        "audit-2026-09-17.jsonl",
        [
            '{"event": "start", "hmac": "aaaa"}',
            '"hmac"',
            '["hmac"]',
            "42",
            "null",
            "true",
            "not json at all",
            '{"event": "stop", "hmac": "bbbb"}',
        ],
    )

    ref, mtime, details = audit_pack._hmac_chain_tail_digest(audit_dir)

    assert ref == "sha256:bbbb"
    assert mtime > 0.0
    assert details["file"] == "audit-2026-09-17.jsonl"
    # Every non-blank line counts, parseable or not.
    assert details["lines"] == 8


def test_tail_digest_reports_no_hmac_when_only_non_dict_lines(tmp_path: Path) -> None:
    """A log of nothing but junk yields the no-hmac branch, not a bogus tail."""
    audit_dir = tmp_path / "audit"
    _write_log(audit_dir, "audit-2026-09-17.jsonl", ['"hmac"', '["hmac"]', "42"])

    ref, mtime, details = audit_pack._hmac_chain_tail_digest(audit_dir)

    assert ref == ""
    assert mtime > 0.0
    assert details["reason"] == "no hmac field found"
    assert details["lines"] == 3


def test_tail_digest_on_a_missing_directory_reports_nothing(tmp_path: Path) -> None:
    """No audit directory is not an error, and carries no details to explain."""
    assert audit_pack._hmac_chain_tail_digest(tmp_path / "absent") == ("", 0.0, {})


def test_tail_digest_on_a_directory_without_logs_says_so(tmp_path: Path) -> None:
    """An empty audit directory is distinguishable from one whose tail is junk."""
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    (audit_dir / "README.md").write_text("not a log\n", encoding="utf-8")

    ref, mtime, details = audit_pack._hmac_chain_tail_digest(audit_dir)

    assert ref == ""
    assert mtime == 0.0
    assert details == {"reason": "no audit log files present"}
