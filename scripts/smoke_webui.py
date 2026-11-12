#!/usr/bin/env python3
"""Exercise the shipped operator UI against a throwaway local GUI server."""

from __future__ import annotations

import secrets
import tempfile
from pathlib import Path
from unittest.mock import patch

from capture_webui_renders import gui_server, resolve_bernstein
from playwright.sync_api import Page, expect, sync_playwright

from bernstein.core.approval.models import PendingApproval
from bernstein.core.approval.queue import ApprovalQueue

ROUTES = {
    "tasks": "Tasks",
    "agents": "Agents",
    "approvals": "Approvals queue",
    "audit": "Audit log",
    "costs": "Costs",
    "missions": "Missions",
    "fleet": "Fleet overview",
    "settings": "Settings",
    "vocabulary": "Reference",
}
TASK_TITLE = "Check operator UI smoke task"


def check_routes(page: Page, base: str, dashboard_password: str, headers: dict[str, str]) -> None:
    expect(page).to_have_url(f"{base}/ui/tasks")
    for route, heading in ROUTES.items():
        response = page.goto(f"{base}/ui/{route}")
        assert response is not None and response.ok, f"Could not load /ui/{route}"
        expect(page.get_by_role("heading", name=heading, exact=True)).to_be_visible()
        print(f"/ui/{route}", flush=True)

    page.goto(f"{base}/ui/governance")
    expect(page.get_by_text("governance coverage", exact=True)).to_be_visible()
    login = page.request.post(f"{base}/api/v1/dashboard/auth/login", data={"password": dashboard_password})
    expect(login).to_be_ok()
    # The legacy page requires a dashboard session, and its API requires a bearer.
    page.set_extra_http_headers(headers)
    with page.expect_response(f"{base}/review-board/runs") as board_runs:
        page.get_by_role("navigation").get_by_role("link", name="Review board", exact=True).click()
    assert board_runs.value.ok, f"Review board returned HTTP {board_runs.value.status}"
    expect(page).to_have_url(f"{base}/dashboard/review-board")
    expect(page.get_by_role("heading", name="Run review board", exact=True)).to_be_visible()
    page.set_extra_http_headers({})

    page.goto(f"{base}/ui/unknown-smoke-route")
    expect(page.get_by_role("heading", name="Route not found")).to_be_visible()


def check_task(page: Page, base: str, task_id: str, headers: dict[str, str]) -> None:
    page.goto(f"{base}/ui/tasks")
    row = page.get_by_role("row").filter(has_text=TASK_TITLE)
    row.get_by_role("button", name="Row actions").click()
    drawer = page.get_by_role("dialog", name=TASK_TITLE)
    for name in ("Diff", "Gates", "Artifacts", "Logs", "Deps", "Trace", "Steer", "Summary"):
        button = drawer.get_by_role("button", name=name, exact=True)
        button.click()
        expect(button).to_have_attribute("aria-pressed", "true")

    cancel_url = f"{base}/api/v1/tasks/{task_id}/cancel"
    with page.expect_response(
        lambda response: response.url == cancel_url and response.request.method == "POST"
    ) as reply:
        drawer.get_by_role("button", name="Cancel run", exact=True).click()
    assert reply.value.ok, f"Task cancellation returned HTTP {reply.value.status}"
    task = page.request.get(f"{base}/api/v1/tasks/{task_id}", headers=headers)
    expect(task).to_be_ok()
    assert task.json()["status"] == "cancelled"


def check_task_creation(page: Page, base: str, headers: dict[str, str]) -> None:
    tasks_url = f"{base}/api/v1/tasks"
    for index, entry in enumerate(("header", "prompt", "empty state"), start=1):
        page.goto(f"{base}/ui/tasks")
        if entry == "empty state":
            page.get_by_role("button", name="Failed").click()
            expect(page.get_by_text("No tasks yet", exact=True)).to_be_visible()
            trigger = page.get_by_role("button", name="Create task", exact=True).last
        elif entry == "prompt":
            trigger = page.get_by_role("button", name="New from prompt", exact=True)
        else:
            trigger = page.get_by_role("button", name="Create task", exact=True)
        trigger.click()
        form = page.get_by_role("dialog", name="Create task", exact=True)
        expect(form.get_by_label("Prompt" if entry == "prompt" else "Title")).to_be_focused()
        if entry == "header":
            page.keyboard.press("Escape")
            expect(trigger).to_be_focused()
            trigger.click()
            expect(form.get_by_label("Title")).to_be_focused()
            page.mouse.click(1, 1)
            expect(form).to_be_hidden()
            expect(trigger).to_be_focused()
            trigger.click()
        title = f"Task creation smoke {index}"
        description = f"Created from the {entry} control."
        form.get_by_label("Title").fill(title)
        form.get_by_label("Prompt").fill(description)
        form.get_by_label("Role (optional)").fill("qa")
        with page.expect_response(lambda reply: reply.url == tasks_url and reply.request.method == "POST") as created:
            form.get_by_role("button", name="Create task", exact=True).click()
        assert created.value.status == 201
        task_id = created.value.json()["id"]
        expect(page.get_by_role("dialog", name=title, exact=True)).to_be_visible()
        task = page.request.get(f"{tasks_url}/{task_id}", headers=headers)
        expect(task).to_be_ok()
        assert (task.json()["title"], task.json()["description"], task.json()["role"]) == (title, description, "qa")


def check_approvals(
    page: Page,
    base: str,
    headers: dict[str, str],
    approvals: list[PendingApproval],
    queue_dir: Path,
) -> None:
    page.goto(f"{base}/ui/approvals")
    expect(page.get_by_role("button", name="Approve once")).to_be_visible()
    expect(page.get_by_role("button", name="Always deny", exact=False)).to_have_count(0)

    for approval, decision in zip(approvals[:2], ("allow", "reject"), strict=True):
        url = f"{base}/api/v1/approvals/{approval.id}/resolve"
        with page.expect_response(url) as reply:
            if decision == "allow":
                page.get_by_role("button", name="Approve once").click()
            else:
                page.keyboard.press("d")
        assert reply.value.ok, f"Approval resolution returned HTTP {reply.value.status}"
        assert reply.value.request.post_data_json == {"decision": decision, "nonce": approval.nonce_hex}
        resolution = ApprovalQueue(base_dir=queue_dir).get_resolution(approval.id)
        assert resolution is not None and resolution.decision.value == decision
        expect(page.get_by_text(f"APPROVAL · {approval.id}", exact=True)).to_be_hidden()

    stale = approvals[2]
    expect(page.get_by_text(f"APPROVAL · {stale.id}", exact=True)).to_be_visible()
    url = f"{base}/api/v1/approvals/{stale.id}/resolve"
    settled = page.request.post(url, data={"decision": "reject", "nonce": stale.nonce_hex}, headers=headers)
    expect(settled).to_be_ok()
    with page.expect_response(url) as reply:
        page.get_by_role("button", name="Approve once").click()
    assert reply.value.status == 410
    expect(page.get_by_role("alert").filter(has_text="This approval is no longer current")).to_be_visible()
    expect(page.get_by_role("button", name="Approve once")).to_be_hidden()


def main() -> None:
    token = secrets.token_urlsafe(32)
    dashboard_password = secrets.token_urlsafe(32)
    headers = {"Authorization": f"Bearer {token}"}
    with tempfile.TemporaryDirectory(prefix="bernstein-ui-smoke-") as directory:
        project = Path(directory)
        queue_dir = project / ".sdd/runtime/approvals"
        queue = ApprovalQueue(base_dir=queue_dir)
        approvals = [
            queue.push(
                PendingApproval(
                    session_id=f"smoke-{index}",
                    agent_role="qa",
                    tool_name="read_file",
                    tool_args={"path": f"docs/check-{index}.md"},
                    ttl_seconds=120,
                )
            )
            for index in range(3)
        ]
        env = {"BERNSTEIN_AUDIT_KEY_PATH": str(project / "audit.key")}
        with (
            patch.dict("os.environ", env),
            gui_server(resolve_bernstein(None), project, token, dashboard_password) as base,
            sync_playwright() as playwright,
        ):
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.set_default_timeout(8_000)
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            created = page.request.post(
                f"{base}/api/v1/tasks",
                headers=headers,
                data={"title": TASK_TITLE, "description": TASK_TITLE, "role": "qa"},
            )
            expect(created).to_be_ok()

            page.goto(f"{base}/ui/")
            token_input = page.get_by_label("Access token", exact=True)
            expect(token_input).to_be_visible()
            token_input.fill(token)
            page.get_by_role("button", name="Save", exact=True).click()
            expect(token_input).to_be_hidden()
            expect(page.get_by_role("row").filter(has_text=TASK_TITLE)).to_be_visible()

            check_routes(page, base, dashboard_password, headers)
            check_task(page, base, created.json()["id"], headers)
            check_approvals(page, base, headers, approvals, queue_dir)
            check_task_creation(page, base, headers)
            assert not errors, "Browser errors:\n" + "\n".join(errors)
            browser.close()
    print("Web UI route, drawer, auth, task and approval smoke passed", flush=True)


if __name__ == "__main__":
    main()
