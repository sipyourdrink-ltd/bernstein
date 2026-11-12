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
                row.press("d")
        assert reply.value.ok, f"Approval resolution returned HTTP {reply.value.status}"
        assert reply.value.request.post_data_json == {"decision": decision, "nonce": approval.nonce_hex}
        resolution = ApprovalQueue(base_dir=queue_dir).get_resolution(approval.id)
        assert resolution is not None and resolution.decision.value == decision
        expect(page.get_by_text(f"APPROVAL · {approval.id}", exact=True)).to_be_hidden()
        assert attempts.count(url) == 1

    stale = approvals[2]
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

    pointer_approval, pending = approvals[3:5]
    expect(page.get_by_text(f"APPROVAL · {pointer_approval.id}", exact=True)).to_be_visible()
    expect(tasks_link).to_be_focused()

    url = f"{base}/api/v1/approvals/{pointer_approval.id}/resolve"
    deferred: list[Route] = []
    page.route(url, lambda route: deferred.append(route), times=1)
    with page.expect_request(url):
        queue_row(pointer_approval).press("d")
    page.get_by_text("Why this needs review", exact=True).click()
    assert page.evaluate("() => document.activeElement === document.body")
    with page.expect_response(url) as pointer_reply:
        deferred[0].continue_()
    assert pointer_reply.value.ok
    expect(page.get_by_text(f"APPROVAL · {pending.id}", exact=True)).to_be_visible()
    assert page.evaluate("() => document.activeElement === document.body")
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

    expired = approvals[5]
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
                    ttl_seconds=0 if index == 5 else 120,
                )
            )
            for index in range(6)
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
            check_governance_receipt(page, base)
            assert not errors, "Browser errors:\n" + "\n".join(errors)
            browser.close()
    print("Web UI route, drawer, authentication, approval, task cancellation and receipt smoke passed", flush=True)


if __name__ == "__main__":
    main()
