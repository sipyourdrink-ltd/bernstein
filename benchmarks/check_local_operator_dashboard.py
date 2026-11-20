"""Exercise the installed operator GUI in a real browser with no agents running.

Run with an installed Bernstein wheel and Playwright Chromium:
    python benchmarks/check_local_operator_dashboard.py
Use --channel chrome to run with an installed Google Chrome instead.
"""

from __future__ import annotations

import argparse
import multiprocessing
import os
import socket
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from playwright.sync_api import expect, sync_playwright

if TYPE_CHECKING:
    from collections.abc import Iterator


def _serve(listener: socket.socket, project: Path, token: str) -> None:
    # Only the task server runs in this isolated project. It has no spawner.
    platform_env = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ}
    os.environ.clear()
    os.environ.update(
        platform_env,
        HOME=str(project),
        USERPROFILE=str(project),
        XDG_CACHE_HOME=str(project / "cache"),
        XDG_CONFIG_HOME=str(project / "config"),
        XDG_DATA_HOME=str(project / "data"),
        PATH=os.defpath,
        BERNSTEIN_AUTH_TOKEN=token,
    )
    os.chdir(project)
    import uvicorn

    server = uvicorn.Server(uvicorn.Config("bernstein.core.server:app", log_level="error"))
    server.run(sockets=[listener])


@contextmanager
def _local_server(project: Path, token: str) -> Iterator[httpx.Client]:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        base = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = multiprocessing.get_context("spawn").Process(target=_serve, args=(listener, project, token))
        server.start()
    try:
        with httpx.Client(base_url=base, headers={"Authorization": f"Bearer {token}"}, trust_env=False) as client:
            deadline = time.monotonic() + 30
            while server.is_alive() and time.monotonic() < deadline:
                try:
                    if client.get("/health/ready", timeout=0.5).is_success:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.05)
            else:
                raise RuntimeError(f"Local task server did not become ready (exit {server.exitcode})")
            yield client
    finally:
        if server.is_alive():
            server.terminate()
        server.join(5)
        if server.is_alive():
            server.kill()
            server.join()
        server.close()


def check_dashboard(client: httpx.Client, token: str, channel: str | None) -> None:
    title = "Read this local task before an agent starts"
    task = client.post("/tasks", json={"title": title, "description": "Local operator fixture"})
    task.raise_for_status()
    task_id = task.json()["id"]
    assert client.get("/dashboard/data").raise_for_status().json()["agents"] == []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=channel)
        page = browser.new_page()
        page.set_default_timeout(10_000)
        try:
            entry = page.goto(str(client.base_url.join("/ui/")), wait_until="domcontentloaded")
            assert entry is not None and entry.ok, f"GUI entry returned {entry and entry.status}"
            page.get_by_label("Access token", exact=True).fill(token)
            page.get_by_role("button", name="Save", exact=True).click()
            row = page.get_by_role("row").filter(has_text=title)
            expect(row).to_be_visible()
            detail_url = str(client.base_url.join(f"/api/v1/tasks/{task_id}"))
            with page.expect_response(detail_url) as detail:
                row.click()
            assert detail.value.ok and detail.value.json()["id"] == task_id
            expect(page.get_by_role("dialog", name=title)).to_be_visible()
            page.get_by_role("button", name="Close detail").click()
            expect(page.get_by_role("dialog", name=title)).to_be_hidden()
        finally:
            browser.close()
    assert client.get("/dashboard/data").raise_for_status().json()["agents"] == []


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", help="Playwright Chromium channel, e.g. chrome")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="bernstein-operator-") as temp:
        session_token = uuid.uuid4().hex
        with _local_server(Path(temp), session_token) as local_client:
            check_dashboard(local_client, session_token, args.channel)
    print("The browser opened and closed a live task detail with zero registered agents.")
