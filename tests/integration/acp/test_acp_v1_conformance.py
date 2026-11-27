"""ACP v1 registry auth-check mirror, stdout purity, and manifest conformance tests.

Mirrors the upstream ``agentclientprotocol/registry`` CI verification surface:

1. Raw JSON-RPC ``run_auth_check`` handshake against ``bernstein acp serve --stdio``
   sending the registry validator's exact ``initialize`` payload (including
   ``clientCapabilities._meta["terminal-auth"] = true``) and asserting a valid
   ``protocolVersion: 1`` response with non-empty valid ``authMethods``
   (``bernstein-setup`` terminal auth). Configured terminal-auth conformance and
   empty/invalid ``authMethods`` rejection are also covered by dedicated unit
   tests below.
2. Strict stdout purity: every single line emitted on ``stdout`` by
   ``bernstein acp serve --stdio`` across a multi-frame session must be a valid
   JSON-RPC 2.0 envelope, with zero banners, log lines, or illegal notification
   replies.
3. Prepared ACP registry assets (``packaging/acp-registry/bernstein/agent.json``
   and ``icon.svg``) satisfy the upstream manifest and 16x16 monochrome SVG
   validation rules (including ``<style>`` blocks and strict allowed color set).
4. The ``agent-client-protocol`` SDK is installed in the dev test environment so
   ``test_acp_v1_sdk.py`` runs rather than skipping silently.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, cast

import pytest

from bernstein.core.protocols.acp.handlers import ACPHandlerRegistry
from bernstein.core.protocols.acp.session import ACPSessionStore
from bernstein.core.protocols.acp.transport import StdioAcpTransport

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = REPO_ROOT / "src"
REGISTRY_DIR = REPO_ROOT / "packaging" / "acp-registry" / "bernstein"
AGENT_JSON_PATH = REGISTRY_DIR / "agent.json"
ICON_SVG_PATH = REGISTRY_DIR / "icon.svg"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"

_ALLOWED_SVG_COLORS = frozenset({"none", "currentcolor", "inherit"})
_STYLE_COLOR_RE = re.compile(r"(?:^|[;{\s])(fill|stroke)\s*:\s*([^;}\n]+)", re.IGNORECASE)
_IMPORTANT_RE = re.compile(r"\s*!important\s*$", re.IGNORECASE)
_SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_ID_RE = re.compile(r"^[a-z][a-z0-9-]*$")

pytestmark = pytest.mark.integration


def _subprocess_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{SRC_DIR}{os.pathsep}{existing}" if existing else str(SRC_DIR)
    env["PYTHONUNBUFFERED"] = "1"
    if extra:
        env.update(extra)
    return env


def _parse_registry_auth_methods(raw_methods: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mirror ``agentclientprotocol/registry/.github/workflows/client.py`` parsing."""
    parsed: list[dict[str, Any]] = []
    for method in raw_methods:
        auth_type = method.get("type")
        if not auth_type:
            meta = method.get("_meta")
            if isinstance(meta, dict):
                if "terminal-auth" in meta:
                    auth_type = "terminal"
                elif "agent-auth" in meta:
                    auth_type = "agent"
        if not auth_type:
            auth_type = "agent"
        parsed.append(
            {
                "id": method.get("id", ""),
                "name": method.get("name", ""),
                "type": auth_type,
                "description": method.get("description"),
            }
        )
    return parsed


def _validate_registry_auth_methods(auth_methods: list[dict[str, Any]]) -> tuple[bool, str]:
    """Mirror ``agentclientprotocol/registry`` auth validation rules."""
    if not auth_methods:
        return False, "No authMethods in response"
    valid = [m for m in auth_methods if m.get("type") in {"agent", "terminal"}]
    if not valid:
        found = [m.get("type") for m in auth_methods]
        return False, f"No auth method with type 'agent' or 'terminal'. Found types: {found}"
    return True, f"Found {len(valid)} valid auth method(s)"


def _parse_pure_jsonrpc_stdout(raw_stdout: bytes) -> list[dict[str, Any]]:
    """Assert every stdout line is a single valid JSON-RPC 2.0 frame."""
    text = raw_stdout.decode("utf-8")
    frames: list[dict[str, Any]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        assert line.strip(), f"stdout line {lineno} is blank (breaks strict JSONL framing)"
        assert "\x1b" not in line, f"stdout line {lineno} contains ANSI escape bytes: {line!r}"
        try:
            raw_frame: object = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AssertionError(f"stdout line {lineno} is not valid JSON ({exc}): {line!r}") from exc
        assert isinstance(raw_frame, dict), f"stdout line {lineno} is not a JSON object: {raw_frame!r}"
        frame = cast("dict[str, Any]", raw_frame)
        assert frame.get("jsonrpc") == "2.0", f"stdout line {lineno} missing jsonrpc='2.0': {frame!r}"
        has_method = "method" in frame
        has_result = "result" in frame
        has_error = "error" in frame
        assert has_method or (has_result != has_error), (
            f"stdout line {lineno} is not a valid JSON-RPC 2.0 envelope: {frame!r}"
        )
        frames.append(frame)
    return frames


def _run_stdio_session(
    frames: list[dict[str, Any]],
    *,
    cli_prefix: tuple[str, ...] = (),
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    stdin_bytes = "".join(json.dumps(f) + "\n" for f in frames).encode("utf-8")
    return subprocess.run(
        [sys.executable, "-m", "bernstein", *cli_prefix, "acp", "serve", "--stdio"],
        input=stdin_bytes,
        capture_output=True,
        env=_subprocess_env(env_extra),
        timeout=30,
        check=False,
    )


def test_registry_run_auth_check_mirror() -> None:
    """Mirror ``agentclientprotocol/registry`` ``run_auth_check`` handshake."""
    init_request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": 1,
            "clientInfo": {"name": "ACP Registry Validator", "version": "1.0.0"},
            "clientCapabilities": {
                "terminal": True,
                "fs": {"readTextFile": True, "writeTextFile": True},
                "_meta": {
                    "terminal_output": True,
                    "terminal-auth": True,
                },
            },
        },
    }
    proc = _run_stdio_session([init_request])
    assert proc.returncode == 0, f"stderr={proc.stderr.decode('utf-8', errors='replace')}"

    responses = _parse_pure_jsonrpc_stdout(proc.stdout)
    assert len(responses) == 1, f"expected exactly 1 response frame, got {len(responses)}"
    resp = responses[0]
    assert resp.get("id") == 1
    assert "error" not in resp, f"Agent error: {resp['error']}"

    result = resp["result"]
    assert result["protocolVersion"] == 1
    assert result["agentInfo"]["name"] == "bernstein"
    assert result["agentInfo"]["version"]

    raw_methods_obj: object = result.get("authMethods", [])
    assert isinstance(raw_methods_obj, list)
    raw_methods = cast("list[dict[str, Any]]", raw_methods_obj)
    assert raw_methods, (
        "default 'bernstein acp serve --stdio' must advertise non-empty authMethods for terminal-auth clients"
    )
    auth_methods = _parse_registry_auth_methods(raw_methods)
    ok, msg = _validate_registry_auth_methods(auth_methods)
    assert ok, msg


def test_registry_run_auth_check_accepts_configured_terminal_auth() -> None:
    """Configured terminal auth on ACPHandlerRegistry passes registry run_auth_check."""

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(loop=loop)
        capture = asyncio.StreamReader(loop=loop)
        capture_proto = asyncio.StreamReaderProtocol(capture, loop=loop)

        class _CaptureTransport(asyncio.WriteTransport):
            def __init__(self) -> None:
                super().__init__()
                self._closed = False

            def write(self, data: bytes | bytearray | memoryview[Any]) -> None:
                if not self._closed:
                    capture.feed_data(bytes(data))

            def close(self) -> None:
                if not self._closed:
                    self._closed = True
                    capture.feed_eof()

            def is_closing(self) -> bool:
                return self._closed

            def can_write_eof(self) -> bool:
                return True

            def write_eof(self) -> None:
                self.close()

        writer = asyncio.StreamWriter(_CaptureTransport(), capture_proto, reader, loop)
        registry = ACPHandlerRegistry(
            sessions=ACPSessionStore(),
            terminal_auth_method={
                "id": "bernstein-setup",
                "name": "Configure Bernstein adapters",
                "type": "terminal",
                "args": ["--setup"],
            },
        )
        transport = StdioAcpTransport(registry=registry, reader=reader, writer=writer)

        init_request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": 1,
                "clientInfo": {"name": "ACP Registry Validator", "version": "1.0.0"},
                "clientCapabilities": {
                    "terminal": True,
                    "fs": {"readTextFile": True, "writeTextFile": True},
                    "_meta": {
                        "terminal_output": True,
                        "terminal-auth": True,
                    },
                },
            },
        }
        reader.feed_data((json.dumps(init_request) + "\n").encode("utf-8"))
        reader.feed_eof()
        await transport.serve_forever()
        writer.close()

        raw_line = await asyncio.wait_for(capture.readline(), timeout=1.0)
        responses = _parse_pure_jsonrpc_stdout(raw_line)
        assert len(responses) == 1
        auth_methods = _parse_registry_auth_methods(responses[0]["result"].get("authMethods", []))
        ok, msg = _validate_registry_auth_methods(auth_methods)
        assert ok, msg

    asyncio.run(_run())


def test_stdio_stdout_purity_across_v1_session(tmp_path: Path) -> None:
    """``bernstein acp serve --stdio`` emits strictly valid JSON-RPC frames on stdout."""
    init_req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": 1,
            "clientCapabilities": {"auth": {"terminal": True}},
            "clientInfo": {"name": "purity-check", "version": "1.0.0"},
        },
    }
    new_sess_req: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "session/new",
        "params": {"cwd": str(tmp_path), "mcpServers": []},
    }
    cancel_notif = {
        "jsonrpc": "2.0",
        "method": "session/cancel",
        "params": {"sessionId": "nonexistent-session"},
    }
    for cli_prefix, env_extra in (
        ((), {"BERNSTEIN_LOG_LEVEL": "DEBUG"}),
        (("--verbose",), {"BERNSTEIN_LOG_JSON": "1"}),
    ):
        proc = _run_stdio_session(
            [init_req, new_sess_req, cancel_notif],
            cli_prefix=cli_prefix,
            env_extra=env_extra,
        )
        assert proc.returncode == 0, f"stderr={proc.stderr.decode('utf-8', errors='replace')}"

        frames = _parse_pure_jsonrpc_stdout(proc.stdout)
        # Two requests + one notification -> exactly two response frames, zero error frames.
        assert [f.get("id") for f in frames] == [1, 2]
        assert all("error" not in f for f in frames), frames
        assert frames[0]["result"]["protocolVersion"] == 1
        assert frames[1]["result"]["sessionId"]


def test_stdout_purity_parser_rejects_non_jsonrpc_noise() -> None:
    """Self-test: the purity checker fails loudly on banners or ANSI sequences."""
    noisy = b"Bernstein starting...\n" + json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode() + b"\n"
    with pytest.raises(AssertionError, match="not valid JSON"):
        _parse_pure_jsonrpc_stdout(noisy)

    ansi = b'\x1b[32m{"jsonrpc":"2.0","id":1,"result":{}}\x1b[0m\n'
    with pytest.raises(AssertionError, match="ANSI escape"):
        _parse_pure_jsonrpc_stdout(ansi)


def test_acp_sdk_dev_dependency_is_installed() -> None:
    """``agent-client-protocol>=1.1`` is declared in dev deps and importable."""
    from acp import PROTOCOL_VERSION

    assert PROTOCOL_VERSION == 1


def test_registry_agent_json_matches_upstream_schema() -> None:
    """Prepared ``packaging/acp-registry/bernstein/agent.json`` satisfies registry schema."""
    assert AGENT_JSON_PATH.is_file(), f"missing registry manifest: {AGENT_JSON_PATH}"
    raw_manifest: object = json.loads(AGENT_JSON_PATH.read_text(encoding="utf-8"))
    assert isinstance(raw_manifest, dict)
    manifest = cast("dict[str, Any]", raw_manifest)

    pyproject = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))
    pkg_version = str(pyproject["project"]["version"])

    for required_key in ("id", "name", "version", "description", "license_url", "distribution"):
        assert required_key in manifest, f"missing required key {required_key!r}"

    assert manifest["id"] == "bernstein"
    assert isinstance(manifest["id"], str) and _ID_RE.fullmatch(manifest["id"])
    assert isinstance(manifest["name"], str) and manifest["name"]
    assert isinstance(manifest["version"], str) and _SEMVER_RE.fullmatch(manifest["version"])
    assert manifest["version"] == pkg_version
    assert isinstance(manifest["description"], str) and manifest["description"]
    assert manifest.get("license") == "Apache-2.0"
    assert str(manifest["license_url"]).startswith("https://")
    # Upstream build_registry.py populates ``icon`` from ``icon.svg`` automatically.
    assert "icon" not in manifest

    raw_dist: object = manifest["distribution"]
    assert isinstance(raw_dist, dict)
    dist = cast("dict[str, Any]", raw_dist)
    assert set(dist.keys()) <= {"binary", "npx", "uvx"}
    assert "uvx" in dist
    raw_uvx: object = dist["uvx"]
    assert isinstance(raw_uvx, dict)
    uvx = cast("dict[str, Any]", raw_uvx)
    assert set(uvx.keys()) <= {"package", "args", "env"}
    assert uvx["package"] == f"bernstein=={pkg_version}"
    assert uvx["args"] == ["acp", "serve", "--stdio"]


def _validate_icon_svg(content: str) -> list[str]:
    """Mirror ``agentclientprotocol/registry/.github/workflows/build_registry.py`` SVG checks."""
    errors: list[str] = []
    if "<!--" in content:
        errors.append("icon.svg must not contain HTML/XML comments")

    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        return [*errors, f"icon.svg is not valid XML: {exc}"]

    tag = root.tag.split("}")[-1] if "}" in root.tag else root.tag
    if tag != "svg":
        errors.append(f"root element must be <svg>, got <{tag}>")
    if root.get("width") != "16":
        errors.append(f"icon.svg width must be '16', got {root.get('width')!r}")
    if root.get("height") != "16":
        errors.append(f"icon.svg height must be '16', got {root.get('height')!r}")
    if root.get("viewBox") != "0 0 16 16":
        errors.append(f"icon.svg viewBox must be '0 0 16 16', got {root.get('viewBox')!r}")

    has_current_color = False
    for elem in root.iter():
        elem_tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        for attr in ("fill", "stroke"):
            val = elem.get(attr)
            if val is None:
                continue
            norm = val.strip().lower()
            if norm == "currentcolor":
                has_current_color = True
            elif norm not in _ALLOWED_SVG_COLORS:
                errors.append(f'Icon has hardcoded {attr}="{val}" (use currentColor or none)')

        inline_style = elem.get("style")
        if inline_style:
            for prop in inline_style.split(";"):
                if ":" not in prop:
                    continue
                k, v = prop.split(":", 1)
                if k.strip().lower() in {"fill", "stroke"}:
                    norm = _IMPORTANT_RE.sub("", v).strip().lower()
                    if norm == "currentcolor":
                        has_current_color = True
                    elif norm not in _ALLOWED_SVG_COLORS:
                        errors.append(f"Icon has hardcoded style {k.strip().lower()}: {v.strip()}")

        if elem_tag.lower() == "style" and elem.text:
            for match in _STYLE_COLOR_RE.finditer(elem.text):
                prop_name = match.group(1).lower()
                raw_val = match.group(2).strip()
                norm = _IMPORTANT_RE.sub("", raw_val).strip().lower()
                if norm == "currentcolor":
                    has_current_color = True
                elif norm not in _ALLOWED_SVG_COLORS:
                    errors.append(f"Icon has hardcoded CSS {prop_name}: {raw_val}")

    if not has_current_color:
        errors.append("icon.svg must use currentColor for monochrome theming")
    return errors


def test_registry_icon_svg_is_16x16_square_monochrome_without_comments() -> None:
    """Prepared ``packaging/acp-registry/bernstein/icon.svg`` satisfies registry icon rules."""
    assert ICON_SVG_PATH.is_file(), f"missing registry icon: {ICON_SVG_PATH}"
    content = ICON_SVG_PATH.read_text(encoding="utf-8")
    errors = _validate_icon_svg(content)
    assert not errors, "; ".join(errors)


def test_registry_auth_method_validator_self_test() -> None:
    """Self-test: empty authMethods, unsupported types, and ``_meta`` inference match upstream."""
    ok, msg = _validate_registry_auth_methods([])
    assert ok is False
    assert msg == "No authMethods in response"

    unsupported = _parse_registry_auth_methods([{"id": "oauth", "name": "OAuth", "type": "oauth"}])
    ok, msg = _validate_registry_auth_methods(unsupported)
    assert ok is False
    assert "oauth" in msg

    inferred = _parse_registry_auth_methods(
        [
            {"id": "t", "name": "Term", "_meta": {"terminal-auth": {"command": "bernstein"}}},
            {"id": "a", "name": "Agent", "_meta": {"agent-auth": True}},
            {"id": "d", "name": "Default"},
        ]
    )
    assert [m["type"] for m in inferred] == ["terminal", "agent", "agent"]
    ok, msg = _validate_registry_auth_methods(inferred)
    assert ok is True


def test_registry_icon_svg_validator_matches_upstream_rules() -> None:
    """Self-test: SVG validator rejects ``transparent`` and hardcoded ``<style>`` CSS colors."""
    assert "transparent" not in _ALLOWED_SVG_COLORS
    assert _validate_icon_svg(ICON_SVG_PATH.read_text(encoding="utf-8")) == []

    transparent_svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16" '
        'fill="transparent"><path d="M0 0h16" stroke="currentColor"/></svg>'
    )
    errors = _validate_icon_svg(transparent_svg)
    assert any("transparent" in err for err in errors), errors

    style_hex_svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">'
        "<style>path { fill: #ff0000; }</style>"
        '<path d="M0 0h16" stroke="currentColor"/></svg>'
    )
    errors = _validate_icon_svg(style_hex_svg)
    assert any("#ff0000" in err for err in errors), errors

    style_current_color_svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">'
        "<style>path { fill: none; stroke: currentColor; }</style>"
        '<path d="M0 0h16"/></svg>'
    )
    assert _validate_icon_svg(style_current_color_svg) == []

    comment_svg = (
        '<!-- bad --><svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" '
        'viewBox="0 0 16 16"><path d="M0 0h16" stroke="currentColor"/></svg>'
    )
    assert any("comment" in err for err in _validate_icon_svg(comment_svg))
