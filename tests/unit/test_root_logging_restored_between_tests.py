"""A test that reconfigures root logging does not change what the next one sees (#6296).

The two tests below run in file order on one worker when the file runs alone.
The first does what ``bernstein --quiet`` does to the process; the second asserts
on a WARNING with no ``caplog.at_level`` pin, which is how the leaked ERROR level
emptied ``caplog`` for tests that had done nothing wrong. Under ``-n`` the pair may
split across workers, where the second passes trivially; run the file alone to
exercise the leak.
"""

from __future__ import annotations

import logging

import pytest


def test_a_quiet_invocation_reconfigures_root_logging() -> None:
    # What ``bernstein --quiet`` does to the process today. Written out rather than
    # called through ``apply_verbosity``, so this keeps testing the harness if that
    # function stops touching the root logger (#6192): any in-process program can.
    logging.basicConfig(level=logging.ERROR, format="%(message)s", force=True)

    assert logging.getLogger().level == logging.ERROR


def test_the_next_test_sees_a_warning_without_pinning_a_level(caplog: pytest.LogCaptureFixture) -> None:
    logging.getLogger("bernstein.core.example").warning("still visible")

    assert logging.getLogger().level != logging.ERROR
    assert [r.getMessage() for r in caplog.records] == ["still visible"]
