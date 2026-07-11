"""Shared OpenAI-compatible endpoint stubs for conformance tests.

Two entry points, one behavior model:

* :class:`FakeTransport` -- in-process transport with the exact call
  signature ``run_conformance`` accepts, for fast unit tests.
* :func:`stub_endpoint_server` -- a real threaded HTTP server speaking the
  same behavior over 127.0.0.1, for CLI and end-to-end tests that cannot
  inject a transport.

Behavior toggles (``tools_ok`` / ``patch_ok`` / ...) let a test flip one
capability off and assert the matching probe rejects deterministically.
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any

from bernstein.core.endpoints.conformance import PATCH_REFERENCE_DIFF

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

_TOOL_ARGUMENTS = json.dumps({"path": "a.py", "line": 1, "message": "ok"})


@dataclass
class EndpointBehavior:
    """Configurable OpenAI-compatible endpoint behavior."""

    model: str = "tiny-coder"
    models_ok: bool = True
    chat_ok: bool = True
    tools_ok: bool = True
    patch_ok: bool = True
    context_ok: bool = True
    hang: bool = False
    requests: list[dict[str, Any]] = field(default_factory=list)

    def handle(self, method: str, path: str, body: bytes | None) -> tuple[int, bytes]:
        """Return ``(status, response_bytes)`` for one request."""
        if method == "GET" and path.rstrip("/").endswith("/models"):
            if not self.models_ok:
                return 500, b'{"error":{"message":"models unavailable"}}'
            payload = {"object": "list", "data": [{"id": self.model, "object": "model"}]}
            return 200, json.dumps(payload).encode("utf-8")
        if method == "POST" and path.rstrip("/").endswith("/chat/completions"):
            request = json.loads(body or b"{}")
            self.requests.append(request)
            if self.hang:
                raise TimeoutError("stub hang")
            return self._chat(request)
        return 404, b'{"error":{"message":"not found"}}'

    def _chat(self, request: dict[str, Any]) -> tuple[int, bytes]:
        prompt = "\n".join(str(m.get("content", "")) for m in request.get("messages", []))
        if not self.chat_ok:
            return 500, b'{"error":{"message":"completion backend unavailable"}}'
        if len(prompt) > 8000:
            if not self.context_ok:
                return 400, b'{"error":{"message":"maximum context length exceeded"}}'
            return 200, _completion_bytes(self.model, "ok")
        if request.get("tools"):
            if not self.tools_ok:
                return 200, _completion_bytes(self.model, "tool calling is not supported")
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "record_finding", "arguments": _TOOL_ARGUMENTS},
                    }
                ],
            }
            return 200, _envelope_bytes(self.model, message)
        if "unified diff" in prompt:
            text = PATCH_REFERENCE_DIFF if self.patch_ok else PATCH_REFERENCE_DIFF.replace("+", "?")
            return 200, _completion_bytes(self.model, text)
        return 200, _completion_bytes(self.model, "ready")


def _completion_bytes(model: str, content: str) -> bytes:
    return _envelope_bytes(model, {"role": "assistant", "content": content})


def _envelope_bytes(model: str, message: dict[str, Any]) -> bytes:
    payload = {
        "id": "chatcmpl-stub",
        "object": "chat.completion",
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
    }
    return json.dumps(payload).encode("utf-8")


class FakeTransport:
    """In-process transport matching the ``run_conformance`` contract."""

    def __init__(self, behavior: EndpointBehavior | None = None) -> None:
        self.behavior = behavior or EndpointBehavior()
        self.calls: list[tuple[str, str]] = []

    def __call__(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> tuple[int, bytes]:
        self.calls.append((method, url))
        path = url.split("://", 1)[-1].split("/", 1)[-1]
        return self.behavior.handle(method, "/" + path, body)


class _StubHandler(BaseHTTPRequestHandler):
    behavior: EndpointBehavior

    def _serve(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        try:
            status, payload = self.behavior.handle(method, self.path, body)
        except TimeoutError:
            # Emulate a hung endpoint: never respond within any budget.
            self.connection.close()
            return
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        self._serve("GET")

    def do_POST(self) -> None:
        self._serve("POST")

    def log_message(self, format: str, *args: object) -> None:
        """Silence request logging in test output."""


@contextmanager
def stub_endpoint_server(behavior: EndpointBehavior | None = None) -> Iterator[str]:
    """Serve *behavior* over HTTP on 127.0.0.1; yield the ``/v1`` base URL."""
    resolved = behavior or EndpointBehavior()
    handler = type("Handler", (_StubHandler,), {"behavior": resolved})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
