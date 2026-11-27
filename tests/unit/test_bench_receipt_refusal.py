"""Pin the receipt field that counts a task as refused."""

import pytest

from bernstein.eval.bench.bundle import is_refusal


@pytest.mark.parametrize(
    ("receipt", "expected"),
    [
        ({"status": "refused", "passed": True}, True),
        ({"status": "completed", "reason": "budget refusal"}, False),
        ({"status": "failed", "passed": False}, False),
        ({}, False),
    ],
)
def test_only_canonical_status_marks_refusal(receipt: dict[str, object], expected: bool) -> None:
    assert is_refusal(receipt) is expected
