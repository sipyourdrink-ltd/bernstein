"""Log-sanitization tests for the SLA receipt file loader (#2696).

``read_receipt_file`` logs a caller-supplied path when a receipt fails to
load.  The path reaches the loader straight from the operator (via
``sla verify <file>``) or from a less-trusted caller, so a value carrying
CR/LF could split the single log line into several and forge additional log
records (log injection).  The sink-side ``sanitize_log`` wrapper escapes
CR/LF at the ``logger.*`` call so the record stays one line even when a
control character reaches it.

These tests assert:

1.  A CR/LF-bearing path is escaped in the emitted log record -- no raw line
    break survives, so the record cannot forge a second log line.  Removing
    the escaper from the call site makes this test fail.
2.  A plain path (no control chars) is logged verbatim -- sanitizing does not
    change the logged meaning for non-malicious input.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from bernstein.core.orchestration.sla_receipt import read_receipt_file

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_LOGGER_NAME = "bernstein.core.orchestration.sla_receipt"
_MSG_PREFIX = "Could not load SLA receipt"


def _load_failure_records(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith(_MSG_PREFIX)
    ]


def test_crlf_path_is_escaped_in_load_failure_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # A file whose name embeds CR/LF plus a forged-looking log line; invalid
    # JSON so the load hits the ``except`` branch that logs the path.
    forged = tmp_path / "receipt\r\nCould not load SLA receipt FORGED.json"
    forged.write_text("not valid json")

    caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)
    assert read_receipt_file(forged) is None

    records = _load_failure_records(caplog)
    # Exactly one log record: the injected newline must not split the line.
    assert len(records) == 1, f"expected one load-failure line, got: {caplog.text}"
    line = records[0]
    assert "\r" not in line
    assert "\n" not in line
    # The escaped forms are present instead of raw control characters.
    assert "\\r\\n" in line


def test_plain_path_is_logged_verbatim(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    plain = tmp_path / "receipt.json"
    plain.write_text("not valid json")

    caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)
    assert read_receipt_file(plain) is None

    records = _load_failure_records(caplog)
    assert len(records) == 1, f"expected one load-failure line, got: {caplog.text}"
    # A path with no control characters is unchanged by the escaper.
    assert str(plain) in records[0]
