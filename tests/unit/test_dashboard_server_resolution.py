"""The TUI has to poll the server this run is actually on.

`bernstein live` used a module constant pinned to port 8052. A run whose port
was taken writes the real one to `.sdd/runtime/server.port`, and `bernstein
demo` runs on 8055, so the dashboard polled a port with nothing behind it,
swallowed the connection error into a log line, and drew three empty panels -
identical on screen to an orchestrator with nothing to do (issue #3444).

The tests drive a real HTTP server on a real ephemeral port rather than
patching `httpx`, because the property under test is "the poll reaches the
server this workspace recorded", and a recorded call argument cannot fail the
way a wrong port does.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from bernstein.cli import dashboard_polling
from bernstein.cli.helpers import persist_server_port

#: What the fake server answers `/status` with; any value the real route would
#: not produce by accident, so a passing test cannot be a coincidence.
STATUS_PAYLOAD = {"total": 7, "done": 3, "agents": [{"id": "backend-fixture", "status": "working"}]}


class _Handler(BaseHTTPRequestHandler):
    #: When set, `/status` answers 500 while every other route stays healthy -
    #: a broken route on a reachable server, which must not read as offline.
    status_route_broken = False
    #: When set, every route answers 401 with a JSON body - the shape that
    #: looks like data to a client that does not check the status code.
    every_route_rejects = False

    def do_GET(self) -> None:
        if type(self).every_route_rejects:
            rejection = json.dumps({"detail": "Unauthorized"}).encode()
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(rejection)))
            self.end_headers()
            self.wfile.write(rejection)
            return
        if self.path.startswith("/status") and type(self).status_route_broken:
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = json.dumps(STATUS_PAYLOAD if self.path.startswith("/status") else []).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        """Keep the test output readable."""


@pytest.fixture
def live_server() -> Iterator[int]:
    """Serve JSON on an ephemeral port for the duration of one test."""
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Resolve against a scratch workspace, never the developer's own."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BERNSTEIN_SERVER_URL", raising=False)
    monkeypatch.delenv("BERNSTEIN_AUTH_TOKEN", raising=False)


def test_the_dashboard_polls_the_port_this_run_persisted(live_server: int) -> None:
    """The failing case: a run on any port other than the hardcoded default."""
    persist_server_port(live_server)

    assert dashboard_polling._get("/status") == STATUS_PAYLOAD


def test_the_environment_variable_outranks_the_persisted_port(
    live_server: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same precedence as `resolve_server_url`, so one command cannot disagree.

    The persisted port here is deliberately dead: if it won, the fetch would
    return None and the assertion would fail rather than pass by luck.
    """
    persist_server_port(1)
    monkeypatch.setenv("BERNSTEIN_SERVER_URL", f"http://127.0.0.1:{live_server}")

    assert dashboard_polling._get("/status") == STATUS_PAYLOAD


def test_the_url_is_resolved_per_call_not_at_import(live_server: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """A dashboard started before the server has to follow it when it appears."""
    assert dashboard_polling._get("/status") is None  # nothing recorded yet

    monkeypatch.setenv("BERNSTEIN_SERVER_URL", f"http://127.0.0.1:{live_server}")

    assert dashboard_polling._get("/status") == STATUS_PAYLOAD


def test_a_full_fetch_against_a_live_server_is_not_reported_unreachable(live_server: int) -> None:
    persist_server_port(live_server)

    data = dashboard_polling._fetch_all()

    assert data["server_unreachable"] is False
    assert data["status"] == STATUS_PAYLOAD


def test_an_unreachable_server_is_carried_in_the_payload() -> None:
    """The panels are empty either way; the difference has to be explicit."""
    persist_server_port(1)

    data = dashboard_polling._fetch_all()

    assert data["server_unreachable"] is True
    assert data["status"] is None
    assert data["tasks"] is None


def test_the_header_says_no_connection_rather_than_describing_an_idle_run() -> None:
    """An empty dashboard and a dead server must not read the same."""
    subtitle = dashboard_polling._build_runtime_subtitle(
        git_branch="main",
        elapsed_s=42,
        done=0,
        total=0,
        worktrees=0,
        restart_count=0,
        unreachable_url="http://127.0.0.1:8055",
    )

    assert "No connection to http://127.0.0.1:8055" in subtitle
    assert "Running for" not in subtitle


def test_a_reachable_server_keeps_the_ordinary_subtitle() -> None:
    subtitle = dashboard_polling._build_runtime_subtitle(
        git_branch="main",
        elapsed_s=42,
        done=3,
        total=7,
        worktrees=2,
        restart_count=0,
    )

    assert "No connection" not in subtitle
    assert "3/7 tasks" in subtitle


def test_a_broken_route_on_a_reachable_server_is_not_reported_offline(
    live_server: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One route erroring is a broken route, not a dead server.

    Blanking the header over it would hide the panels that did load, which is
    the opposite of what the unreachable state is for.
    """
    persist_server_port(live_server)
    monkeypatch.setattr(_Handler, "status_route_broken", True)

    data = dashboard_polling._fetch_all()

    assert data["server_unreachable"] is False
    assert data["status"] is None  # the broken route still reports nothing
    assert data["tasks"] == []  # ...while the healthy ones still answer


def test_the_classic_view_polls_the_same_server_as_the_textual_one(live_server: int) -> None:
    """`bernstein live --classic` was pinned to the default port too.

    It resolved through a different import, which is how one command ended up
    answering the same question two ways.
    """
    from bernstein.cli.live import LiveView

    persist_server_port(live_server)

    assert LiveView()._get("/status") == STATUS_PAYLOAD


def test_an_explicit_url_still_wins_for_the_classic_view(live_server: int) -> None:
    """Resolving per request must not take away pointing it somewhere."""
    from bernstein.cli.live import LiveView

    persist_server_port(1)

    assert LiveView(server_url=f"http://127.0.0.1:{live_server}")._get("/status") == STATUS_PAYLOAD


def test_a_json_error_body_is_not_mistaken_for_data(live_server: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """A 401 carrying JSON is the shape that slips past an unchecked read.

    Without a status check the widgets would render `{"detail": ...}` as
    dashboard state, and every route answering that way would still count as a
    reachable server.
    """
    persist_server_port(live_server)
    monkeypatch.setattr(_Handler, "every_route_rejects", True)

    data = dashboard_polling._fetch_all()

    assert data["server_unreachable"] is True
    assert data["status"] is None
    assert data["tasks"] is None


def test_the_run_token_is_only_sent_to_this_machine(
    live_server: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The persisted token is a credential minted for a local run.

    Now that the destination comes from `BERNSTEIN_SERVER_URL`, attaching it
    unconditionally would hand that credential to whatever host the variable
    names - including one set by something other than the operator.
    """
    from bernstein.core.run_auth_token import persist_run_auth_token

    persist_run_auth_token(Path.cwd(), "secret-run-token")

    assert dashboard_polling._auth_headers(f"http://127.0.0.1:{live_server}") == {
        "Authorization": "Bearer secret-run-token"
    }
    assert dashboard_polling._auth_headers("https://elsewhere.example/api") == {}

    # A token the operator set themselves is theirs to aim.
    monkeypatch.setenv("BERNSTEIN_AUTH_TOKEN", "operator-token")
    assert dashboard_polling._auth_headers("https://elsewhere.example/api") == {
        "Authorization": "Bearer operator-token"
    }


def test_the_classic_view_also_withholds_the_run_token_from_a_remote_host(tmp_path: Path) -> None:
    """Both views share one policy, because they now share one resolver."""
    from bernstein.cli.live import LiveView
    from bernstein.core.run_auth_token import persist_run_auth_token

    persist_run_auth_token(Path.cwd(), "secret-run-token")
    view = LiveView(server_url="https://elsewhere.example")

    assert view._get("/status") is None  # nothing there, and nothing sent
    assert dashboard_polling._auth_headers(view._resolved_url()) == {}


def test_one_dashboard_update_reads_from_one_server(live_server: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """A run restarting onto another port must not blend two servers into one frame.

    The resolution happens once per batch, so a port file that changes midway
    lands in the next update rather than half of this one.
    """
    persist_server_port(live_server)
    seen: list[str] = []
    real_get = dashboard_polling._get

    def recording(path: str, url: str | None = None):  # type: ignore[no-untyped-def]
        seen.append(url or "<resolved per call>")
        # Move the target under the batch's feet, the way a restart would.
        persist_server_port(1)
        return real_get(path, url)

    monkeypatch.setattr(dashboard_polling, "_get", recording)
    data = dashboard_polling._fetch_all()

    assert len(set(seen)) == 1, seen
    assert data["status"] == STATUS_PAYLOAD


def test_the_classic_view_says_no_connection_too(live_server: int) -> None:
    """The empty-panels problem is the same in both views, so is the answer."""
    from bernstein.cli.live import LiveView

    view = LiveView(server_url=f"http://127.0.0.1:{live_server}")
    view._render(view._fetch())
    assert view._unreachable_url == ""

    dead = LiveView(server_url="http://127.0.0.1:1")
    frame = dead._render(dead._fetch())

    assert dead._unreachable_url == "http://127.0.0.1:1"
    rendered = "".join(segment.text for segment in frame.renderables[0].render(None))  # type: ignore[attr-defined]
    assert "No connection to http://127.0.0.1:1" in rendered
