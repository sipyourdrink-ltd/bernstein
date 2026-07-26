"""Hermetic tests for `bernstein endpoints certify` / `verify`.

Spins up an in-process fake OpenAI-compatible HTTP server.
No external service needed.

The product modules are imported at module scope on purpose. `_run_certify`
and `_run_verify` used to catch `ImportError` and fall back to
reimplementations defined in this file, so blocking
`bernstein.core.endpoints.certify` and `.verify` still left 22 of 30 tests
passing - against the test file's own shims, with the product unimportable.
An ImportError here now fails collection, which is the honest signal.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from bernstein.core.endpoints.certify import certify_endpoint
from bernstein.core.endpoints.conformance import PATCH_REFERENCE_DIFF
from bernstein.core.endpoints.verify import verify_cert

# ---------------------------------------------------------------------------
# Fake OpenAI-compatible server
# ---------------------------------------------------------------------------

_FAKE_MODELS_RESPONSE = {
    "object": "list",
    "data": [{"id": "test-model", "object": "model", "created": 1700000000, "owned_by": "bernstein-test"}],
}

_FAKE_CHAT_RESPONSE = {
    "id": "chatcmpl-test001",
    "object": "chat.completion",
    "created": 1700000000,
    "model": "test-model",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "pong"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
}

_FAKE_TOOL_CALL_RESPONSE = {
    "id": "chatcmpl-tc01",
    "object": "chat.completion",
    "created": 1700000000,
    "model": "test-model",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_abc",
                        "type": "function",
                        "function": {
                            "name": "record_finding",
                            "arguments": '{"path":"src/app.py","line":12,"message":"unused import"}',
                        },
                    }
                ],
            },
            "finish_reason": "tool_calls",
        }
    ],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}

_STREAM_CHUNKS = [
    b'data: {"id":"s01","object":"chat.completion.chunk","created":1700000000,"model":"test-model","choices":[{"index":0,"delta":{"role":"assistant","content":"po"},"finish_reason":null}]}\n\n',
    b'data: {"id":"s01","object":"chat.completion.chunk","created":1700000000,"model":"test-model","choices":[{"index":0,"delta":{"content":"ng"},"finish_reason":null}]}\n\n',
    b'data: {"id":"s01","object":"chat.completion.chunk","created":1700000000,"model":"test-model","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n',
    b"data: [DONE]\n\n",
]


class _FakeOAIHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        if self.path == "/v1/models":
            body = json.dumps(_FAKE_MODELS_RESPONSE).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self) -> None:
        if self.path != "/v1/chat/completions":
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        body: dict[str, Any] = json.loads(raw)
        stream: bool = body.get("stream", False)
        tools = body.get("tools")

        if stream:
            # Build full response body first, send with Content-Length
            # so urllib doesn't close the connection early
            full = b"".join(_STREAM_CHUNKS)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(full)))
            self.end_headers()
            self.wfile.write(full)
            self.wfile.flush()
        elif tools:
            resp_body = json.dumps(_FAKE_TOOL_CALL_RESPONSE).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp_body)))
            self.end_headers()
            self.wfile.write(resp_body)
            self.wfile.flush()
        else:
            prompt = "\n".join(str(m.get("content", "")) for m in body.get("messages", []))
            if "unified diff" in prompt:
                # Patch-fidelity probe: return the reference diff byte-exactly.
                patch_resp = {
                    "id": "chatcmpl-patch01",
                    "object": "chat.completion",
                    "created": 1700000000,
                    "model": "test-model",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": PATCH_REFERENCE_DIFF},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 40, "total_tokens": 60},
                }
                resp_body = json.dumps(patch_resp).encode()
            else:
                resp_body = json.dumps(_FAKE_CHAT_RESPONSE).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp_body)))
            self.end_headers()
            self.wfile.write(resp_body)
            self.wfile.flush()


@pytest.fixture(scope="module")
def fake_oai_server():
    server = HTTPServer(("127.0.0.1", 0), _FakeOAIHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}/v1"
    server.shutdown()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_certify(base_url, model, out_dir, token="", strict=False, timeout=5):
    return certify_endpoint(
        base_url=base_url, token=token, model=model, out_dir=out_dir, strict=strict, timeout=timeout
    )


def _run_verify(cert_path):
    return verify_cert(cert_path=cert_path)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCertifyBasicPass:
    def test_record_schema(self, fake_oai_server, tmp_path):
        record = _run_certify(fake_oai_server, "test-model", tmp_path / "certs")
        assert record["schema_version"] == 1

    def test_base_url_preserved(self, fake_oai_server, tmp_path):
        record = _run_certify(fake_oai_server, "test-model", tmp_path / "certs")
        assert record["base_url"] == fake_oai_server

    def test_model_preserved(self, fake_oai_server, tmp_path):
        record = _run_certify(fake_oai_server, "test-model", tmp_path / "certs")
        assert record["model"] == "test-model"

    def test_overall_passed(self, fake_oai_server, tmp_path):
        record = _run_certify(fake_oai_server, "test-model", tmp_path / "certs")
        assert record["passed"] is True

    def test_six_probes_present(self, fake_oai_server, tmp_path):
        record = _run_certify(fake_oai_server, "test-model", tmp_path / "certs")
        assert len(record["probes"]) == 6

    def test_required_probes_all_pass(self, fake_oai_server, tmp_path):
        record = _run_certify(fake_oai_server, "test-model", tmp_path / "certs")
        failures = [p for p in record["probes"] if not p["passed"]]
        assert failures == []

    def test_cert_file_written(self, fake_oai_server, tmp_path):
        out_dir = tmp_path / "certs"
        _run_certify(fake_oai_server, "test-model", out_dir)
        assert len(list(out_dir.glob("*.json"))) == 1

    def test_cert_file_is_valid_json(self, fake_oai_server, tmp_path):
        out_dir = tmp_path / "certs"
        _run_certify(fake_oai_server, "test-model", out_dir)
        content = json.loads(next(out_dir.glob("*.json")).read_text())
        assert isinstance(content, dict)

    def test_signature_field_present(self, fake_oai_server, tmp_path):
        record = _run_certify(fake_oai_server, "test-model", tmp_path / "certs")
        assert "signature" in record

    def test_install_key_fp_present(self, fake_oai_server, tmp_path):
        record = _run_certify(fake_oai_server, "test-model", tmp_path / "certs")
        assert record["signer_public_key_pem"]


class TestCertifyProbeDetails:
    def _probe(self, record, name):
        return next(p for p in record["probes"] if p["probe"] == name)

    def test_reachability_probe_passes(self, fake_oai_server, tmp_path):
        record = _run_certify(fake_oai_server, "test-model", tmp_path / "certs")
        assert self._probe(record, "reachability")["passed"] is True

    def test_chat_completion_probe_passes(self, fake_oai_server, tmp_path):
        record = _run_certify(fake_oai_server, "test-model", tmp_path / "certs")
        assert self._probe(record, "chat_completion")["passed"] is True

    def test_tool_calling_probe_passes(self, fake_oai_server, tmp_path):
        record = _run_certify(fake_oai_server, "test-model", tmp_path / "certs")
        assert self._probe(record, "tool_calling")["passed"] is True

    def test_patch_fidelity_probe_passes(self, fake_oai_server, tmp_path):
        record = _run_certify(fake_oai_server, "test-model", tmp_path / "certs")
        assert self._probe(record, "patch_fidelity")["passed"] is True

    def test_timeout_behavior_probe_passes(self, fake_oai_server, tmp_path):
        record = _run_certify(fake_oai_server, "test-model", tmp_path / "certs")
        assert self._probe(record, "timeout_behavior")["passed"] is True

    def test_context_floor_probe_passes(self, fake_oai_server, tmp_path):
        record = _run_certify(fake_oai_server, "test-model", tmp_path / "certs")
        assert self._probe(record, "context_floor")["passed"] is True


class TestVerifyLoop:
    def test_verify_accepts_good_cert(self, fake_oai_server, tmp_path):
        out_dir = tmp_path / "certs"
        _run_certify(fake_oai_server, "test-model", out_dir)
        result = _run_verify(next(out_dir.glob("*.json")))
        assert result["valid"] is True

    def test_verify_rejects_tampered_base_url(self, fake_oai_server, tmp_path):
        out_dir = tmp_path / "certs"
        _run_certify(fake_oai_server, "test-model", out_dir)
        cert_path = next(out_dir.glob("*.json"))
        record = json.loads(cert_path.read_text())
        record["base_url"] = "http://evil.example.com/v1"
        cert_path.write_text(json.dumps(record))
        # Editing a signed field breaks the Ed25519 signature over the
        # canonical binding, so verification fails closed and deterministically.
        result = _run_verify(cert_path)
        assert result["valid"] is False

    def test_verify_cert_preserves_base_url(self, fake_oai_server, tmp_path):
        out_dir = tmp_path / "certs"
        _run_certify(fake_oai_server, "test-model", out_dir)
        result = _run_verify(next(out_dir.glob("*.json")))
        assert result["record"]["base_url"] == fake_oai_server

    def test_verify_cert_preserves_model(self, fake_oai_server, tmp_path):
        out_dir = tmp_path / "certs"
        _run_certify(fake_oai_server, "test-model", out_dir)
        result = _run_verify(next(out_dir.glob("*.json")))
        assert result["record"]["model"] == "test-model"

    def test_verify_cert_passed_true(self, fake_oai_server, tmp_path):
        out_dir = tmp_path / "certs"
        _run_certify(fake_oai_server, "test-model", out_dir)
        result = _run_verify(next(out_dir.glob("*.json")))
        assert result["record"]["passed"] is True

    def test_verify_cert_has_all_probes(self, fake_oai_server, tmp_path):
        out_dir = tmp_path / "certs"
        _run_certify(fake_oai_server, "test-model", out_dir)
        result = _run_verify(next(out_dir.glob("*.json")))
        assert len(result["record"]["probes"]) == 6


class TestCertifyStrictMode:
    def test_strict_passes_when_tool_calls_supported(self, fake_oai_server, tmp_path):
        record = _run_certify(fake_oai_server, "test-model", tmp_path / "certs", strict=True)
        assert record["passed"] is True

    def test_non_strict_passes_even_if_optional_fails(self, tmp_path):
        # Server that returns 200 for everything but no tool_calls in response
        class _NoToolServer(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass

            def do_GET(self):
                if self.path == "/v1/models":
                    body = json.dumps(_FAKE_MODELS_RESPONSE).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    self.wfile.flush()
                else:
                    self.send_response(404)
                    self.end_headers()

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw)
                if body.get("stream"):
                    full = b"".join(_STREAM_CHUNKS)
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(full)))
                    self.end_headers()
                    self.wfile.write(full)
                    self.wfile.flush()
                else:
                    resp = json.dumps(_FAKE_CHAT_RESPONSE).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(resp)))
                    self.end_headers()
                    self.wfile.write(resp)
                    self.wfile.flush()

        server = HTTPServer(("127.0.0.1", 0), _NoToolServer)
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            record = _run_certify(f"http://127.0.0.1:{port}/v1", "test-model", tmp_path / "certs", strict=False)
            assert record["passed"] is True
        finally:
            server.shutdown()


class TestCertifyBadServer:
    def test_unreachable_server_marks_failed(self, tmp_path):
        record = _run_certify("http://127.0.0.1:1/v1", "test-model", tmp_path / "certs", timeout=1)
        assert record["passed"] is False

    def test_unreachable_all_probes_failed(self, tmp_path):
        record = _run_certify("http://127.0.0.1:1/v1", "test-model", tmp_path / "certs", timeout=1)
        assert all(not p["passed"] for p in record["probes"])


class TestCertifyDifferentModels:
    @pytest.mark.parametrize("model", ["llama3", "mistral-7b-instruct", "qwen2.5-coder-7b"])
    def test_model_name_stored(self, fake_oai_server, tmp_path, model):
        record = _run_certify(fake_oai_server, model, tmp_path / f"certs-{model}")
        assert record["model"] == model


class TestIntegrationsListEntry:
    def test_use_case_entry_exists(self):
        # Imported here rather than guarded by a skip: a use-case catalogue
        # that stopped importing is a product regression, not a reason for
        # this test to report itself green without checking anything.
        from bernstein.adapters.use_cases import USE_CASES

        if isinstance(USE_CASES, dict):
            assert "self-hosted-endpoints" in USE_CASES, "USE_CASES missing 'self-hosted-endpoints' key"
            entry = USE_CASES["self-hosted-endpoints"]
        else:
            names = [getattr(uc, "name", None) for uc in USE_CASES]
            assert "self-hosted-endpoints" in names
            entry = next(uc for uc in USE_CASES if getattr(uc, "name", None) == "self-hosted-endpoints")

        # AdapterUseCase uses docs_path, not adapters field
        docs = getattr(entry, "docs_path", "") or ""
        assert "self-hosted-endpoints" in docs, f"docs_path should reference self-hosted-endpoints, got: {docs!r}"
