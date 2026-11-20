"""The lookup smoke must tolerate either ordering of save and busy checks."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


@pytest.mark.parametrize("save_finishes", ["during busy checks", "after busy checks"])
def test_lookup_waits_for_create_handler(monkeypatch: pytest.MonkeyPatch, save_finishes: str) -> None:
    monkeypatch.syspath_prepend(str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("smoke_webui_under_test", SCRIPTS / "smoke_webui.py")
    assert spec is not None and spec.loader is not None
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)

    base = "http://127.0.0.1:1234"
    tasks_url = f"{base}/api/v1/tasks"
    task_id = "a" * 32
    page = MagicMock()
    page.expect_request.return_value.__enter__.return_value.value.method = "GET"
    create_route = MagicMock()
    create_route.request.post_data_json = {"id": task_id}
    create_route.fetch.return_value.status = 201
    create_route.fetch.return_value.json.return_value = {"id": task_id}
    check_route = MagicMock()
    handlers: dict[str, Any] = {}

    def register_route(url: str, handler: Any, **_kwargs: Any) -> None:
        handlers[url] = handler
        if url == f"{tasks_url}/{task_id}":
            handler(check_route)

    page.route.side_effect = register_route
    assertions = MagicMock()
    monkeypatch.setattr(smoke, "expect", lambda _subject: assertions)
    completed = False

    def complete_save(*_args: Any, **_kwargs: Any) -> None:
        nonlocal completed
        if not completed:
            completed = True
            handlers[tasks_url](create_route)

    if save_finishes == "during busy checks":
        assertions.to_have_attribute.side_effect = complete_save
    else:
        assertions.to_contain_text.side_effect = complete_save

    def deliver_response(**kwargs: Any) -> None:
        # The response must stay held throughout the first busy keyboard checks.
        assert [call.args[0] for call in page.keyboard.press.call_args_list] == [
            "Tab",
            "Shift+Tab",
            "Control+k",
            "Enter",
        ]
        assert kwargs == {"status": 500, "json": {"detail": "The task response could not be delivered."}}

    create_route.fulfill.side_effect = deliver_response
    smoke.check_unconfirmed_task_lookup(page, base, {"Authorization": "Bearer test"})

    assert completed
    create_route.fetch.assert_called_once_with()
    create_route.fulfill.assert_called_once()
    check_route.fulfill.assert_called_once_with(status=404, json={"detail": "Not found"})
