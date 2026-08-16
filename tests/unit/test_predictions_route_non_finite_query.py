"""``GET /metrics/predictions`` refuses a non-finite ``budget_cap``.

``budget_cap`` is a USD ceiling declared as ``Query(ge=0.0)``. That bound
does not exclude ``+Infinity`` -- ``inf >= 0.0`` is true -- so the value
passed validation, reached the handler, and was echoed straight back into
the response body as ``budget_cap_usd``. Starlette's ``JSONResponse``
serialises with ``json.dumps(..., allow_nan=False)``, which raises
``ValueError: Out of range float values are not JSON compliant`` on a
non-finite float. The caller got an unhandled 500 for what is really a
malformed request. ``NaN`` fails the same way at the renderer; it slips
past ``ge`` because every comparison against ``NaN`` is false, so the
bound never rejects it.

These cases go through the ASGI stack rather than calling the handler
directly: the defect is that validation admits the value in the first
place, and only a real request exercises the validation layer. The
client is built with ``raise_server_exceptions=False`` so an unhandled
handler exception surfaces as the 500 the operator would actually see,
instead of propagating into the test and masking the status code.

The route's Schemathesis smoke job generates ``Infinity`` only
occasionally, so this crash reached main behind a green suite. These
cases pin it deterministically.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bernstein.core.server import create_app

pytestmark = pytest.mark.ci

# Every spelling of a non-finite float that Python's ``float()`` accepts,
# which is what FastAPI's query coercion ultimately calls.
NON_FINITE = ["Infinity", "-Infinity", "NaN", "inf", "-inf", "nan"]


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = create_app(jsonl_path=tmp_path / "tasks.jsonl")
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("value", NON_FINITE)
def test_non_finite_budget_cap_is_rejected_not_a_server_error(client: TestClient, value: str) -> None:
    """A non-finite budget ceiling is a bad request, never a 500."""
    response = client.get("/metrics/predictions", params={"budget_cap": value})

    assert response.status_code == 422, (
        f"budget_cap={value} returned {response.status_code}; a non-finite ceiling must be "
        "refused at validation, not crash the JSON renderer downstream"
    )


@pytest.mark.parametrize("value", NON_FINITE)
def test_non_finite_window_hours_is_rejected_not_a_server_error(client: TestClient, value: str) -> None:
    """``window_hours`` is echoed back too, so it needs the same guard.

    ``le=72.0`` already excludes ``+Infinity``, but ``-Infinity`` clears
    it and ``NaN`` clears both bounds, so the range alone is not enough.
    """
    response = client.get("/metrics/predictions", params={"window_hours": value})

    assert response.status_code == 422, (
        f"window_hours={value} returned {response.status_code}; a non-finite window must be "
        "refused at validation, not crash the JSON renderer downstream"
    )


@pytest.mark.parametrize("value", ["0", "0.0", "12.5", "1e9"])
def test_finite_budget_cap_still_answers(client: TestClient, value: str) -> None:
    """The refusal is scoped to non-finite input; ordinary caps still work."""
    response = client.get("/metrics/predictions", params={"budget_cap": value})

    assert response.status_code == 200
    assert response.json()["budget_cap_usd"] == float(value)
