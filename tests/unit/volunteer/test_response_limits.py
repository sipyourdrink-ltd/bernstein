"""Volunteer HTTP responses must stay bounded before JSON decoding."""

from __future__ import annotations

import io
import json
from types import SimpleNamespace
from typing import Any

import pytest

from bernstein.core.volunteer.review_task import create_volunteer_review_task
from bernstein.core.volunteer.runner import _validate_open_source_preflight

LIMIT = 1024 * 1024


class Response(io.BytesIO):
    status = 200

    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.consumed = 0

    def read(self, size: int | None = -1) -> bytes:
        assert size is not None and 0 < size <= 65536, "HTTP reads must request a bounded chunk"
        data = super().read(min(size, 4093))
        self.consumed += len(data)
        return data


def payload(value: dict[str, Any], length: int) -> bytes:
    raw = json.dumps(value).encode()
    return raw + b" " * (length - len(raw))


@pytest.mark.parametrize("endpoint", ["visibility", "license", "review"])
@pytest.mark.parametrize("length", [64, LIMIT, LIMIT + 1])
def test_response_limit(monkeypatch: pytest.MonkeyPatch, endpoint: str, length: int) -> None:
    responses: list[Response] = []

    def urlopen(request: Any, *, timeout: int) -> Response:
        value: dict[str, Any]
        if endpoint == "review":
            value = {"id": "review-1"}
            size = length
        elif request.full_url.endswith("/license"):
            value = {"spdx_id": "Apache-2.0"}
            size = length if endpoint == "license" else 64
        else:
            value = {"private": False}
            size = length if endpoint == "visibility" else 64
        response = Response(payload(value, size))
        responses.append(response)
        return response

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    if endpoint == "review":
        bundle: Any = SimpleNamespace(
            task=SimpleNamespace(issue_number=1, commit_sha="abc", repo="owner/repo"),
            worker_keyid="worker",
            digest="digest",
            manifest_sha256="manifest",
            adapter_id="adapter",
            model_id="model",
            created_at="now",
            sandbox_profile="profile",
        )
        result = create_volunteer_review_task(
            bundle, "https://github.com/owner/repo/pull/1", server_url="http://localhost:8052", auth_token="test-token"
        )
        assert result == ("review-1" if length <= LIMIT else None)
    else:
        result = _validate_open_source_preflight("https://github.com/owner/repo", "Apache-2.0")
        assert (result is None) == (length <= LIMIT)
    assert all(response.closed for response in responses)
    assert all(response.consumed <= LIMIT + 1 for response in responses)
