#!/usr/bin/env python3
"""Exercise the shipped operator UI against a throwaway local GUI server."""

from __future__ import annotations

import hashlib
import secrets
import tempfile
from pathlib import Path
from unittest.mock import patch

from capture_webui_renders import gui_server, resolve_bernstein
from playwright.sync_api import Locator, Page, Request, Route, expect, sync_playwright

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


def check_governance_receipt(page: Page, base: str) -> None:
    page.goto(f"{base}/ui/governance")
    panel = page.get_by_role("region", name="Verify a receipt")
    file_input = panel.get_by_label("Receipt file")
    submit = panel.get_by_role("button", name="Verify receipt", exact=True)
    endpoint = f"{base}/api/v1/governance/verify-receipt"
    vectors = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "receipt-vectors"
    valid = vectors / "valid-run-receipt.json"
    tampered = (vectors / "tampered-run-receipt.json").read_bytes()
    # Chromium's inspector omits File upload bytes. Observe the browser fetch
    # arguments and clone its real response without changing the request.
    page.evaluate(
        """() => {
            const fetch = window.fetch.bind(window);
            window.receiptRequests = [];
            window.fetch = (url, init) => {
                const response = fetch(url, init);
                if (typeof url === 'string' && url.endsWith('/governance/verify-receipt')) {
                    const hash = new Response(init.body).arrayBuffer()
                        .then(bytes => crypto.subtle.digest('SHA-256', bytes))
                        .then(bytes => Array.from(
                            new Uint8Array(bytes), byte => byte.toString(16).padStart(2, '0')
                        ).join(''));
                    window.receiptRequests.push(Promise.all([hash, response.then(reply => reply.clone().json())]));
                }
                return response;
            };
        }"""
    )
    expect(submit).to_be_disabled()
    with page.expect_file_chooser() as chooser:
        file_input.press("Enter")
    chooser.value.set_files(valid)
    expect(panel.get_by_text(valid.name, exact=True)).to_be_visible()
    expect(file_input).to_have_accessible_description(
        f"Drop a receipt here or choose a file. Selected file {valid.name}"
    )
    assert page.evaluate("window.receiptRequests.length") == 0, "Selecting a receipt must not submit it"
    with page.expect_response(endpoint) as passed:
        submit.press("Enter")
    assert passed.value.ok
    digest, verdict = page.evaluate("() => Promise.all(window.receiptRequests)")[-1]
    assert digest == hashlib.sha256(valid.read_bytes()).hexdigest()
    assert verdict["tier"] == "integrity-only"
    expect(panel.get_by_text("Receipt integrity verified", exact=True)).to_be_visible()
    key_caveat = panel.get_by_text("The signed receipt contents were checked", exact=False)
    expect(key_caveat).to_contain_text("public key embedded in the receipt")
    expect(key_caveat).to_contain_text("operator's public key obtained separately")
    expect(panel.get_by_text(verdict["run_id"], exact=True)).to_be_visible()

    reformatted = valid.read_bytes() + b" \n"
    file_input.set_input_files({"name": "reformatted.json", "mimeType": "application/json", "buffer": reformatted})
    expect(key_caveat).to_be_hidden()
    with page.expect_response(endpoint) as reformatted_response:
        submit.click()
    assert reformatted_response.value.ok
    digest, reformatted_verdict = page.evaluate("() => Promise.all(window.receiptRequests)")[-1]
    assert digest == hashlib.sha256(reformatted).hexdigest()
    assert reformatted_verdict["tier"] == "integrity-only"
    expect(key_caveat).to_be_visible()

    data_transfer = page.evaluate_handle(
        """bytes => {
            const transfer = new DataTransfer();
            transfer.items.add(new File([new Uint8Array(bytes)], 'tampered.json'));
            return transfer;
        }""",
        list(tampered),
    )
    panel.get_by_text("Drop a receipt here or choose a file.", exact=True).dispatch_event(
        "drop", {"dataTransfer": data_transfer}
    )
    expect(file_input).to_have_accessible_description(
        "Drop a receipt here or choose a file. Selected file tampered.json"
    )
    expect(panel.get_by_text("Receipt integrity verified", exact=True)).to_be_hidden()
    with page.expect_response(endpoint) as failed:
        submit.click()
    assert failed.value.ok
    digest, failed_verdict = page.evaluate("() => Promise.all(window.receiptRequests)")[-1]
    assert digest == hashlib.sha256(tampered).hexdigest()
    assert failed_verdict["status"] == "tampered"
    expect(panel.get_by_text("Receipt did not verify", exact=True)).to_be_visible()
    expect(panel.get_by_text("tampered", exact=True)).to_be_visible()
    expect(key_caveat).to_be_hidden()

    file_input.set_input_files({"name": "empty.json", "mimeType": "application/json", "buffer": b""})
    page.route(endpoint, lambda route: route.fulfill(status=503, json={"detail": "Verifier unavailable"}))
    try:
        with page.expect_response(endpoint) as unavailable:
            submit.click()
        assert unavailable.value.status == 503
        expect(panel.get_by_role("alert")).to_contain_text("Could not verify the receipt")
    finally:
        page.unroute(endpoint)
    with page.expect_response(endpoint) as malformed:
        panel.get_by_role("button", name="Retry verification", exact=True).click()
    assert malformed.value.ok
    receipts = page.evaluate("() => Promise.all(window.receiptRequests)")
    digest, malformed_verdict = receipts[-1]
    assert digest == hashlib.sha256(b"").hexdigest()
    assert malformed_verdict["status"] == "malformed"
    expect(panel.get_by_text("malformed", exact=True)).to_be_visible()
    expect(panel.get_by_role("alert")).to_have_count(0)
    assert len(receipts) == 5


def check_trace_highlight_contrast(page: Page, drawer: Locator, base: str, task_id: str) -> None:
    trace_url = f"{base}/api/v1/dashboard/tasks/{task_id}/trace?**"
    timestamp = 1_768_000_000
    response = {
        "task_id": task_id,
        "events": [
            {
                "id": "smoke-trace-event",
                "ts": timestamp,
                "kind": "verify",
                "actor": "qa",
                "summary": "Verified smoke task",
                "outcome": "success",
                "trace_id": "smoke-trace",
                "session_id": "smoke-session",
                "payload": {},
            }
        ],
        "total": 1,
        "cursor": None,
        "first_ts": timestamp,
        "last_ts": timestamp,
        "has_open_trace": False,
    }
    page.route(trace_url, lambda route: route.fulfill(json=response))
    settled_colors = page.add_style_tag(content="* { transition: none !important; }")
    was_dark = page.evaluate("() => document.documentElement.classList.contains('dark')")
    try:
        drawer.get_by_role("button", name="Trace", exact=True).click()
        drawer.get_by_role("searchbox", name="Search trace events").fill("smoke")
        mark = drawer.locator("mark").filter(has_text="smoke")
        expect(mark).to_be_visible()
        for dark in (False, True):
            page.evaluate("(dark) => document.documentElement.classList.toggle('dark', dark)", dark)
            ratio = mark.evaluate(
                r"""mark => {
                    const sample = document.createElement('canvas').getContext('2d');
                    const rgba = color => {
                        if (/^rgba?\(/.test(color)) {
                            const [red, green, blue, alpha = 1] = color.match(/[\d.]+/g).map(Number);
                            return [red, green, blue, alpha];
                        }
                        // Convert other CSS color spaces into sRGB in the browser.
                        sample.clearRect(0, 0, 1, 1);
                        sample.fillStyle = color;
                        sample.fillRect(0, 0, 1, 1);
                        const [red, green, blue, alpha] = sample.getImageData(0, 0, 1, 1).data;
                        return [red, green, blue, alpha / 255];
                    };
                    const over = (top, bottom) => {
                        const alpha = top[3] + bottom[3] * (1 - top[3]);
                        if (alpha === 0) return [0, 0, 0, 0];
                        return [
                            ...top.slice(0, 3).map((channel, i) =>
                                (channel * top[3] + bottom[i] * bottom[3] * (1 - top[3])) / alpha),
                            alpha,
                        ];
                    };
                    let ink = rgba(getComputedStyle(mark).color);
                    let paper = [0, 0, 0, 0];
                    for (let node = mark; node; node = node.parentElement) {
                        const style = getComputedStyle(node);
                        const background = rgba(style.backgroundColor);
                        ink = over(ink, background);
                        paper = over(paper, background);
                        ink[3] *= Number(style.opacity);
                        paper[3] *= Number(style.opacity);
                    }
                    const luminance = color => color
                        .slice(0, 3)
                        .map(channel => channel / 255)
                        .map(channel => channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4)
                        .reduce((sum, channel, i) => sum + channel * [0.2126, 0.7152, 0.0722][i], 0);
                    const viewport = [255, 255, 255, 1];
                    const foreground = luminance(over(ink, viewport));
                    const backdrop = luminance(over(paper, viewport));
                    return (Math.max(foreground, backdrop) + 0.05) / (Math.min(foreground, backdrop) + 0.05);
                }"""
            )
            theme = "dark" if dark else "light"
            assert ratio >= 4.5, f"Trace highlight contrast {ratio:.2f}:1 in {theme} theme"
            print(f"Trace highlight contrast {ratio:.2f}:1 in {theme} theme", flush=True)
    finally:
        page.evaluate("(dark) => document.documentElement.classList.toggle('dark', dark)", was_dark)
        settled_colors.evaluate("style => style.remove()")
        page.unroute(trace_url)


def check_task(page: Page, base: str, task_id: str, headers: dict[str, str]) -> None:
    page.goto(f"{base}/ui/tasks")
    row = page.get_by_role("row").filter(has_text=TASK_TITLE)
    row.get_by_role("button", name="Row actions").click()
    drawer = page.get_by_role("dialog", name=TASK_TITLE)
    for name in ("Diff", "Gates", "Artifacts", "Logs", "Deps", "Trace", "Steer", "Summary"):
        button = drawer.get_by_role("button", name=name, exact=True)
        button.click()
        expect(button).to_have_attribute("aria-pressed", "true")

    check_trace_highlight_contrast(page, drawer, base, task_id)
    drawer.get_by_role("button", name="Summary", exact=True).click()

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
        page.keyboard.press("Escape")
        return_target = (
            page.get_by_role("button", name="Create task", exact=True).first if entry == "empty state" else trigger
        )
        expect(return_target).to_be_focused()


def check_task_creation_failures(page: Page, base: str, headers: dict[str, str]) -> None:
    tasks_url = f"{base}/api/v1/tasks"
    events_url = f"{base}/api/v1/events"
    page.route(events_url, lambda route: route.fulfill(content_type="text/event-stream", body=": no events\n\n"))
    page.goto(f"{base}/ui/tasks")
    trigger = page.get_by_role("button", name="Create task", exact=True).first
    trigger.click()
    form = page.get_by_role("dialog", name="Create task", exact=True)
    submit = form.get_by_role("button", name="Create task", exact=True)
    cancel = form.get_by_role("button", name="Cancel", exact=True)
    form.get_by_label("Title").fill("Task creation response loss")
    form.get_by_label("Prompt").fill("Confirm the task list before repeating this request.")
    role = form.get_by_label("Role (optional)")
    role.fill("unknown")

    page.context.set_offline(True)
    try:
        submit.click()
        expect(form.get_by_role("alert")).to_contain_text("You are offline")
        expect(cancel).to_be_enabled()
    finally:
        page.context.set_offline(False)

    saved_ids: list[str] = []

    def respond_to_create(route: Route) -> None:
        if route.request.method != "POST":
            route.continue_()
        elif route.request.post_data_json.get("role") == "unknown":
            route.fulfill(status=400, json={"detail": "Role unknown is not configured. Allowed roles: qa."})
        elif route.request.post_data_json["title"] == "Malformed task response":
            route.fulfill(status=201, json={"id": {"unexpected": True}})
        elif route.request.post_data_json["title"] == "Padded task ID":
            route.fulfill(status=201, json={"id": " 0123456789ab "})
        elif route.request.post_data_json["title"] == "Invalid task ID":
            route.fulfill(status=201, json={"id": "wrong-id"})
        else:
            response = route.fetch()
            assert response.status == 201
            saved_ids.append(response.json()["id"])
            route.fulfill(status=500, json={"detail": "The task response could not be delivered."})

    page.route(tasks_url, respond_to_create)
    try:
        submit.click()
        expect(form.get_by_role("alert")).to_contain_text("Allowed roles: qa.")
        expect(role).to_be_enabled()
        role.fill("qa")
        submit.click()
        expect(form.get_by_role("alert")).to_contain_text("Check the task before retrying")
        expect(submit).to_be_disabled()
        assert len(saved_ids) == 1
        expect(page.request.get(f"{tasks_url}/{saved_ids[0]}", headers=headers)).to_be_ok()
        cancel.click()
        expect(trigger).to_be_focused()
        expect(page.get_by_text("Task creation response loss", exact=True)).to_be_visible()
        trigger.click()
        form.get_by_role("button", name="Check task", exact=True).click()
        expect(page.get_by_role("dialog", name="Task creation response loss", exact=True)).to_be_visible()
        page.keyboard.press("Escape")
        expect(trigger).to_be_focused()

        for title in ("Malformed task response", "Padded task ID", "Invalid task ID"):
            trigger.click()
            form.get_by_label("Title").fill(title)
            form.get_by_label("Prompt").fill("Treat a malformed response as unconfirmed.")
            submit.click()
            expect(form.get_by_role("alert")).to_contain_text("Check the task before retrying")
            expect(submit).to_be_disabled()
            form.get_by_role("button", name="Start a new task instead", exact=True).click()
            expect(submit).to_be_enabled()
            cancel.click()
            expect(trigger).to_be_focused()
    finally:
        page.unroute(tasks_url, respond_to_create)
        page.unroute(events_url)


def check_task_creation_during_initial_list(page: Page, base: str, headers: dict[str, str]) -> None:
    tasks_url = f"{base}/api/v1/tasks"
    list_url = f"{tasks_url}?page=1"
    events_url = f"{base}/api/v1/events"
    header = page.get_by_role("button", name="Create task", exact=True).first
    form = page.get_by_role("dialog", name="Create task", exact=True)

    def unconfirmed_create(route: Route) -> None:
        response = route.fetch()
        assert response.status == 201
        route.fulfill(status=500, json={"detail": "The task response could not be delivered."})

    def submit_from(trigger: Locator, title: str) -> None:
        trigger.click()
        form.get_by_label("Title").fill(title)
        form.get_by_label("Prompt").fill("Check the refreshed list after response loss.")
        form.get_by_role("button", name="Create task", exact=True).click()
        expect(form.get_by_role("alert")).to_contain_text("Check the task before retrying")

    page.route(events_url, lambda route: route.fulfill(content_type="text/event-stream", body=": no events\n\n"))
    page.route(tasks_url, unconfirmed_create)
    try:
        snapshot = page.request.get(list_url, headers=headers)
        expect(snapshot).to_be_ok()
        stale_routes: list[Route] = []
        page.route(list_url, lambda route: stale_routes.append(route), times=1)
        with page.expect_request(list_url):
            page.goto(f"{base}/ui/tasks")
        title = "Created while the initial list was loading"
        submit_from(header, title)
        assert len(stale_routes) == 1
        with page.expect_request(list_url):
            form.get_by_role("button", name="Cancel", exact=True).click()
        stale_routes[0].fulfill(response=snapshot)
        expect(page.get_by_role("row").filter(has_text=title)).to_be_visible()
        expect(header).to_be_focused()
        header.click()
        form.get_by_role("button", name="Check task", exact=True).click()
        expect(page.get_by_role("dialog", name=title, exact=True)).to_be_visible()
        page.keyboard.press("Escape")

        title = "Created from an empty task list"
        page.route(list_url, lambda route: route.fulfill(json=[]), times=1)
        page.reload()
        expect(page.get_by_text("No tasks yet", exact=True)).to_be_visible()
        submit_from(page.get_by_role("button", name="Create task", exact=True).last, title)
        form.get_by_role("button", name="Cancel", exact=True).click()
        expect(page.get_by_role("row").filter(has_text=title)).to_be_visible()
        expect(header).to_be_focused()
        header.click()
        form.get_by_role("button", name="Check task", exact=True).click()
        expect(page.get_by_role("dialog", name=title, exact=True)).to_be_visible()
        page.keyboard.press("Escape")
    finally:
        page.unroute(tasks_url, unconfirmed_create)
        page.unroute(list_url)
        page.unroute(events_url)


def check_unconfirmed_task_lookup(page: Page, base: str, headers: dict[str, str]) -> None:
    tasks_url = f"{base}/api/v1/tasks"
    title = "Task saved outside the current filter"
    saved_ids: list[str] = []
    pending_create: list[Route] = []
    pending_check: list[Route] = []

    def respond_to_create(route: Route) -> None:
        response = route.fetch()
        assert response.status == 201
        task_id = response.json()["id"]
        assert route.request.post_data_json["id"] == task_id
        saved_ids.append(task_id)
        pending_create.append(route)

    page.goto(f"{base}/ui/tasks")
    page.get_by_role("button", name="Failed").click()
    expect(page.get_by_text("No tasks yet", exact=True)).to_be_visible()
    header = page.get_by_role("button", name="Create task", exact=True).first
    page.get_by_role("button", name="Create task", exact=True).last.click()
    form = page.get_by_role("dialog", name="Create task", exact=True)
    form.get_by_label("Title").fill(title)
    form.get_by_label("Prompt").fill("Look up the exact task despite response loss.")
    page.route(tasks_url, respond_to_create, times=1)
    try:
        submit = form.get_by_role("button", name="Create task", exact=True)
        submit.press("Enter")
        expect(submit).to_have_attribute("aria-disabled", "true")
        expect(submit).to_be_focused()
        page.mouse.click(1, 1)
        expect(form).to_be_visible()
        for key in ("Tab", "Shift+Tab", "Control+k", "Enter"):
            page.keyboard.press(key)
            expect(submit).to_be_focused()
        expect(page.get_by_role("dialog", name="Command palette")).to_have_count(0)
        assert len(saved_ids) == len(pending_create) == 1
        pending_create[0].fulfill(status=500, json={"detail": "The task response could not be delivered."})
        expect(form.get_by_role("alert")).to_contain_text("Check the task before retrying")
        task_url = f"{tasks_url}/{saved_ids[0]}"
        page.route(task_url, lambda route: pending_check.append(route), times=1)
        check = form.get_by_role("button", name="Check task", exact=True)
        expect(check).to_be_focused()
        with page.expect_request(task_url):
            check.press("Enter")
        expect(check).to_have_attribute("aria-disabled", "true")
        page.mouse.click(1, 1)
        expect(form).to_be_visible()
        for key in ("Tab", "Shift+Tab", "Control+k", "Enter"):
            page.keyboard.press(key)
            expect(check).to_be_focused()
        expect(page.get_by_role("dialog", name="Command palette")).to_have_count(0)
        assert len(pending_check) == 1
        pending_check[0].fulfill(status=404, json={"detail": "Not found"})
        expect(form.get_by_role("alert")).to_contain_text("The task is not visible yet")
        expect(form.get_by_role("button", name="Create task", exact=True)).to_be_disabled()
        form.get_by_role("button", name="Cancel", exact=True).click()
        expect(header).to_be_focused()
        header.click()
        expect(form.get_by_label("Title")).to_have_value(title)
        expect(check).to_be_focused()
        expect(form.get_by_role("button", name="Create task", exact=True)).to_be_disabled()
        page.reload()
        page.get_by_role("button", name="Failed").click()
        header.click()
        expect(check).to_be_focused()
        expect(form.get_by_role("button", name="Create task", exact=True)).to_be_disabled()
        with page.expect_request(task_url) as readback:
            check.click()
        assert readback.value.method == "GET"
        expect(page.get_by_role("dialog", name=title, exact=True)).to_be_visible()
        expect(page.request.get(task_url, headers=headers)).to_be_ok()
        assert len(saved_ids) == 1
        page.keyboard.press("Escape")
        expect(header).to_be_focused()
        expect(page.get_by_text("No tasks yet", exact=True)).to_be_visible()
    finally:
        page.unroute(tasks_url, respond_to_create)


def check_task_creation_survives_route_remount(page: Page, base: str) -> None:
    tasks_url = f"{base}/api/v1/tasks"
    title = "Creation in progress during navigation"
    pending: list[Route] = []
    attempts: list[str] = []

    def hold_create(route: Route) -> None:
        attempts.append(route.request.post_data_json["id"])
        pending.append(route)

    page.goto(f"{base}/ui/settings")
    page.get_by_role("navigation").get_by_role("link", name="Tasks", exact=True).click()
    header = page.get_by_role("button", name="Create task", exact=True).first
    header.click()
    form = page.get_by_role("dialog", name="Create task", exact=True)
    form.get_by_label("Title").fill(title)
    form.get_by_label("Prompt").fill("Complete the original request after returning.")
    page.route(tasks_url, hold_create)
    try:
        with page.expect_request(lambda request: request.url == tasks_url and request.method == "POST"):
            form.get_by_role("button", name="Create task", exact=True).click()
        page.go_back()
        expect(page).to_have_url(f"{base}/ui/settings")
        page.go_forward()
        expect(page).to_have_url(f"{base}/ui/tasks")
        header.click()
        expect(form.get_by_text("Creating task...", exact=True)).to_be_visible()
        expect(form.get_by_role("button", name="Start a new task instead", exact=True)).to_be_disabled()
        expect(form.get_by_role("button", name="Check task", exact=True)).to_be_disabled()
        assert len(attempts) == len(pending) == 1
        pending[0].fulfill(status=400, json={"detail": "Choose the qa role."})
        expect(form.get_by_role("alert")).to_have_text("Choose the qa role.")
        expect(form.get_by_label("Title")).to_be_focused()
        expect(form.get_by_label("Title")).to_have_value(title)
        role = form.get_by_label("Role (optional)")
        expect(role).to_be_enabled()
        expect(form.get_by_role("button", name="Check task", exact=True)).to_have_count(0)
        role.fill("qa")
        with page.expect_request(lambda request: request.url == tasks_url and request.method == "POST"):
            form.get_by_role("button", name="Create task", exact=True).click()
        expect(form.get_by_text("Creating task...", exact=True)).to_be_visible()
        assert len(attempts) == len(pending) == 2
        assert attempts[0] != attempts[1]
        pending[1].fulfill(response=pending[1].fetch())
        expect(page.get_by_role("dialog", name=title, exact=True)).to_be_visible()
        page.keyboard.press("Escape")
    finally:
        page.unroute(tasks_url, hold_create)


def check_task_creation_rejected_while_away(page: Page, base: str) -> None:
    tasks_url = f"{base}/api/v1/tasks"
    title = "Task rejected while the operator is away"
    prompt = "Keep the original draft available for correction."
    pending: list[Route] = []
    page.goto(f"{base}/ui/settings")
    page.get_by_role("navigation").get_by_role("link", name="Tasks", exact=True).click()
    header = page.get_by_role("button", name="Create task", exact=True).first
    header.click()
    form = page.get_by_role("dialog", name="Create task", exact=True)
    form.get_by_label("Title").fill(title)
    form.get_by_label("Prompt").fill(prompt)
    form.get_by_label("Role (optional)").fill("incorrect")
    page.route(tasks_url, lambda route: pending.append(route), times=1)
    try:
        with page.expect_request(lambda request: request.url == tasks_url and request.method == "POST"):
            form.get_by_role("button", name="Create task", exact=True).click()
        page.go_back()
        expect(page).to_have_url(f"{base}/ui/settings")
        assert len(pending) == 1
        pending[0].fulfill(status=400, json={"detail": "Choose the qa role."})
        page.wait_for_function("sessionStorage.getItem('bernstein.unconfirmed-task-id') === null")
        page.go_forward()
        expect(page).to_have_url(f"{base}/ui/tasks")
        header.click()
        expect(form.get_by_role("alert")).to_have_text("Choose the qa role.")
        expect(form.get_by_label("Title")).to_be_focused()
        expect(form.get_by_label("Title")).to_have_value(title)
        expect(form.get_by_label("Prompt")).to_have_value(prompt)
        expect(form.get_by_label("Role (optional)")).to_have_value("incorrect")
        expect(form.get_by_role("button", name="Check task", exact=True)).to_have_count(0)
        form.get_by_role("button", name="Cancel", exact=True).click()
    finally:
        page.unroute(tasks_url)


def check_task_creation_success_after_remount(page: Page, base: str, *, settlement: str) -> None:
    tasks_url = f"{base}/api/v1/tasks"
    title = f"Task confirmed after returning to Tasks ({settlement})"
    pending: list[Route] = []
    page.goto(f"{base}/ui/settings")
    page.get_by_role("navigation").get_by_role("link", name="Tasks", exact=True).click()
    header = page.get_by_role("button", name="Create task", exact=True).first
    header.click()
    form = page.get_by_role("dialog", name="Create task", exact=True)
    form.get_by_label("Title").fill(title)
    form.get_by_label("Prompt").fill("Open the confirmed task when the operator returns.")
    page.route(tasks_url, lambda route: pending.append(route), times=1)
    try:
        with page.expect_request(lambda request: request.url == tasks_url and request.method == "POST"):
            form.get_by_role("button", name="Create task", exact=True).click()
        page.go_back()
        expect(page).to_have_url(f"{base}/ui/settings")
        assert len(pending) == 1
        if settlement == "while away":
            pending[0].fulfill(response=pending[0].fetch())
            expect(page.get_by_role("heading", name="Settings", exact=True)).to_be_visible()
        page.go_forward()
        expect(page).to_have_url(f"{base}/ui/tasks")
        palette = page.get_by_role("dialog", name="Command palette")
        if settlement == "with form open":
            header.click()
            expect(form.get_by_text("Creating task...", exact=True)).to_be_visible()
        elif settlement == "with palette open":
            page.keyboard.press("Control+k")
            expect(palette.get_by_role("textbox")).to_be_focused()
        else:
            expect(form).to_have_count(0)
        if settlement != "while away":
            pending[0].fulfill(response=pending[0].fetch())
        detail = page.get_by_role("dialog", name=title, exact=True)
        if settlement != "with form open":
            expect(page.get_by_role("status").filter(has_text="Task created.")).to_have_count(1)
            expect(detail).to_have_count(0)
            if settlement == "with palette open":
                expect(palette.get_by_role("textbox")).to_be_focused()
                page.keyboard.press("Escape")
                expect(palette).to_have_count(0)
            view_created = page.get_by_role("button", name="View created task", exact=True)
            if settlement == "with form closed":
                page.get_by_role("button", name="Done", exact=False).first.click()
                header.click()
                expect(form.get_by_label("Title")).to_have_value("")
                form.get_by_role("button", name="Cancel", exact=True).click()
                expect(view_created).to_be_visible()
            view_created.click()
        expect(detail).to_be_visible()
        assert page.evaluate("sessionStorage.getItem('bernstein.unconfirmed-task-id')") is None
        page.keyboard.press("Escape")
        expect(header).to_be_focused()
    finally:
        page.unroute(tasks_url)


def check_task_creation_empty_state_focus(page: Page, base: str) -> None:
    list_url = f"{base}/api/v1/tasks?page=1"
    events_url = f"{base}/api/v1/events"
    page.route(events_url, lambda route: route.fulfill(content_type="text/event-stream", body=": no events\n\n"))
    try:
        page.route(list_url, lambda route: route.fulfill(json=[]), times=1)
        page.goto(f"{base}/ui/tasks")
        expect(page.get_by_text("No tasks yet", exact=True)).to_be_visible()
        trigger = page.get_by_role("button", name="Create task", exact=True)
        trigger.last.click()
        form = page.get_by_role("dialog", name="Create task", exact=True)
        form.get_by_role("button", name="Cancel", exact=True).click()
        expect(trigger.first).to_be_focused()
        trigger.last.click()
        title = "Created before the empty task list refreshed"
        form.get_by_label("Title").fill(title)
        form.get_by_label("Prompt").fill("Keep focus on a stable control after closing the drawer.")
        deferred: list[Route] = []
        page.route(list_url, lambda route: deferred.append(route), times=1)
        with page.expect_request(list_url):
            form.get_by_role("button", name="Create task", exact=True).click()
        expect(page.get_by_role("dialog", name=title, exact=True)).to_be_visible()
        assert len(deferred) == 1
        page.keyboard.press("Escape")
        deferred[0].continue_()
        expect(page.get_by_role("row").filter(has_text=title)).to_be_visible()
        expect(trigger).to_be_focused()
    finally:
        page.unroute(list_url)
        page.unroute(events_url)


def check_approvals(
    page: Page,
    base: str,
    headers: dict[str, str],
    approvals: list[PendingApproval],
    queue_dir: Path,
) -> None:
    page.goto(f"{base}/ui/approvals")
    approve = page.get_by_role("button", name="Approve once")
    expect(approve).to_be_visible()
    expect(page.get_by_role("button", name="Always deny", exact=False)).to_have_count(0)
    attempts: list[str] = []

    def record_resolution(request: Request) -> None:
        if request.method == "POST" and request.url.endswith("/resolve"):
            attempts.append(request.url)

    page.on("request", record_resolution)

    queue = page.get_by_role("list", name="Pending approvals")

    def queue_row(approval: PendingApproval) -> Locator:
        return queue.get_by_role("button").filter(has_text=approval.session_id)

    def defer_decision(row: Locator, key: str, url: str) -> Route:
        deferred: list[Route] = []
        page.route(url, lambda route: deferred.append(route), times=1)
        with page.expect_request(url):
            row.press(key)
        # The request event may arrive before Playwright invokes its route handler.
        for _ in range(100):
            if deferred:
                break
            page.wait_for_timeout(10)
        assert len(deferred) == 1
        return deferred[0]

    first, second = map(queue_row, approvals[:2])
    expect(first).to_have_attribute("aria-current", "true")
    approve.press("d")
    second.press("d")
    expect(first).to_have_attribute("aria-current", "true")
    assert not attempts
    first.press("ArrowDown")
    expect(second).to_be_focused()
    expect(second).to_have_attribute("aria-current", "true")
    second.press("ArrowUp")
    expect(first).to_be_focused()
    expect(first).to_have_attribute("aria-current", "true")

    for approval, decision in zip(approvals[:2], ("allow", "reject"), strict=True):
        expect(page.get_by_text(f"APPROVAL · {approval.id}", exact=True)).to_be_visible()
        expect(approve).to_be_enabled()
        row = queue_row(approval)
        row.focus()
        url = f"{base}/api/v1/approvals/{approval.id}/resolve"
        with page.expect_response(url) as reply:
            if decision == "allow":
                approve.evaluate("""button => {
                    button.click();
                    document.activeElement.dispatchEvent(new KeyboardEvent('keydown', { key: 'd', bubbles: true }));
                }""")
            else:
                request = defer_decision(row, "d", url)
                row.press("ArrowDown")
                page.keyboard.press("ArrowDown")
                expect(queue_row(approvals[3])).to_have_attribute("aria-current", "true")
                page.keyboard.press("Shift+Tab")
                page.keyboard.press("Shift+Tab")
                expect(row).to_be_focused()
                request.continue_()
        assert reply.value.ok, f"Approval resolution returned HTTP {reply.value.status}"
        assert reply.value.request.post_data_json == {"decision": decision, "nonce": approval.nonce_hex}
        resolution = ApprovalQueue(base_dir=queue_dir).get_resolution(approval.id)
        assert resolution is not None and resolution.decision.value == decision
        expect(page.get_by_text(f"APPROVAL · {approval.id}", exact=True)).to_be_hidden()
        assert attempts.count(url) == 1

    stale = approvals[2]
    selected_while_pending = queue_row(approvals[3])
    expect(selected_while_pending).to_be_focused()
    expect(selected_while_pending).to_have_attribute("aria-current", "true")
    selected_while_pending.press("ArrowUp")
    expect(page.get_by_text(f"APPROVAL · {stale.id}", exact=True)).to_be_visible()
    expect(queue_row(stale)).to_be_focused()
    expect(queue_row(stale)).to_have_attribute("aria-current", "true")
    queue_url = f"{base}/api/v1/approvals/queue"
    nav = page.get_by_role("navigation")
    nav.get_by_role("link", name="Tasks", exact=True).click()
    page.route(queue_url, lambda route: route.fulfill(status=503, json={"detail": "Queue unavailable"}))
    page.evaluate("() => { const now = Date.now; Date.now = () => now() + 6_000; }")
    with page.expect_response(queue_url) as queue_error:
        nav.get_by_role("link", name="Approvals").click()
    assert queue_error.value.status == 503
    expect(page.get_by_text("Could not load approvals", exact=True)).to_be_visible(timeout=25_000)
    page.keyboard.press("a")
    assert len(attempts) == 2
    page.unroute(queue_url)
    page.get_by_role("button", name="Retry", exact=True).click()
    expect(approve).to_be_visible()

    url = f"{base}/api/v1/approvals/{stale.id}/resolve"
    settled = page.request.post(url, data={"decision": "reject", "nonce": stale.nonce_hex}, headers=headers)
    expect(settled).to_be_ok()
    tasks_link = nav.get_by_role("link", name="Tasks", exact=True)
    stale_row = queue_row(stale)
    stale_row.focus()
    with page.expect_response(url) as reply:
        stale_row.evaluate(
            """(row, next) => {
                row.dispatchEvent(new KeyboardEvent('keydown', { key: 'a', bubbles: true }));
                next.focus();
            }""",
            tasks_link.element_handle(),
        )
    assert reply.value.status == 410
    expect(page.get_by_role("alert").filter(has_text="This approval is no longer current")).to_be_visible()
    expect(page.get_by_text(f"APPROVAL · {stale.id}", exact=True)).to_be_hidden()

    pointer_approval, pending, last_row = approvals[3:6]
    expect(page.get_by_text(f"APPROVAL · {pointer_approval.id}", exact=True)).to_be_visible()
    expect(tasks_link).to_be_focused()

    url = f"{base}/api/v1/approvals/{pointer_approval.id}/resolve"
    request = defer_decision(queue_row(pointer_approval), "d", url)
    page.get_by_text("Why this needs review", exact=True).click()
    assert page.evaluate("() => document.activeElement === document.body")
    with page.expect_response(url) as pointer_reply:
        request.continue_()
    assert pointer_reply.value.ok
    expect(page.get_by_text(f"APPROVAL · {pending.id}", exact=True)).to_be_visible()
    assert page.evaluate("() => document.activeElement === document.body")

    row = queue_row(last_row)
    row.click()
    expect(page.get_by_text(f"APPROVAL · {last_row.id}", exact=True)).to_be_visible()
    url = f"{base}/api/v1/approvals/{last_row.id}/resolve"
    request = defer_decision(row, "d", url)
    expect(approve).to_be_disabled()
    page.keyboard.press("Tab")
    assert page.evaluate("() => !document.hasFocus() && document.activeElement === document.body")
    with page.expect_response(url) as tab_reply:
        request.continue_()
    assert tab_reply.value.ok
    expect(page.get_by_text(f"APPROVAL · {pending.id}", exact=True)).to_be_visible()
    assert page.evaluate("() => !document.hasFocus() && document.activeElement === document.body")

    page.evaluate("() => { const now = Date.now; Date.now = () => now() + 180_000; }")
    url = f"{base}/api/v1/approvals/{pending.id}/resolve"
    expect(approve).to_be_enabled()
    with page.expect_response(url) as skewed:
        queue_row(pending).press("a")
    assert skewed.value.ok
    resolution = ApprovalQueue(base_dir=queue_dir).get_resolution(pending.id)
    assert resolution is not None and resolution.decision.value == "allow"
    expect(page.get_by_text(f"APPROVAL · {pending.id}", exact=True)).to_be_hidden()
    assert attempts.count(url) == 1

    expect(page.get_by_role("heading", name="Approvals queue")).to_be_focused()

    expired = approvals[6]
    expect(page.get_by_text(f"APPROVAL · {expired.id}", exact=True)).to_be_hidden()
    reply = page.request.post(
        f"{base}/api/v1/approvals/{expired.id}/resolve",
        data={"decision": "allow", "nonce": expired.nonce_hex},
        headers=headers,
    )
    assert reply.status == 410
    assert reply.json()["detail"] == "NONCE_EXPIRED"
    assert ApprovalQueue(base_dir=queue_dir).get_resolution(expired.id) is None
    page.remove_listener("request", record_resolution)


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
                    ttl_seconds=0 if index == 6 else 120,
                )
            )
            for index in range(7)
        ]
        env = {"BERNSTEIN_AUDIT_KEY_PATH": str(project / "audit.key")}
        with (
            patch.dict("os.environ", env),
            gui_server(resolve_bernstein(None), project, token, dashboard_password) as base,
            sync_playwright() as playwright,
        ):
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 900}, service_workers="block")
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
            check_task_creation_failures(page, base, headers)
            check_task_creation_during_initial_list(page, base, headers)
            check_unconfirmed_task_lookup(page, base, headers)
            check_task_creation_survives_route_remount(page, base)
            check_task_creation_rejected_while_away(page, base)
            for settlement in ("while away", "with form closed", "with form open", "with palette open"):
                check_task_creation_success_after_remount(page, base, settlement=settlement)
            check_task_creation(page, base, headers)
            check_task_creation_empty_state_focus(page, base)
            check_governance_receipt(page, base)
            assert not errors, "Browser errors:\n" + "\n".join(errors)
            browser.close()
    print("Web UI route, drawer, auth, task, approval and receipt smoke passed", flush=True)


if __name__ == "__main__":
    main()
