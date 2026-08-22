"""The repo-root MCP bridge declaration must be runnable by any MCP client.

``.mcp.json`` is what a generic MCP client reads to start the Bernstein
bridge, and ``mcp.json`` is the Agent Plugins 1.0.0 projection of it. Neither
is interpreted by Bernstein's own plugin manager, so neither may lean on a
plugin-manager variable or on a build tool that is absent from a plain
install (issue #4315).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

#: ``${VAR}`` / ``$VAR`` style placeholders. Bernstein's plugin manager
#: expands these for hook scripts it runs itself; an MCP client does not,
#: and passes the literal text straight through to the OS.
_PLACEHOLDER = re.compile(r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?")

#: Commands a client can only run when an extra tool happens to be installed.
#: A bridge declaration has to work on a plain ``pip install bernstein``.
_BUILD_TOOLS = {"uv", "uvx", "poetry", "pdm", "hatch", "pipenv", "rye"}

BRIDGE_MANIFESTS = (".mcp.json", "mcp.json")


def _servers(name: str) -> dict[str, dict]:
    return json.loads((REPO / name).read_text(encoding="utf-8"))["mcpServers"]


@pytest.mark.parametrize("manifest", BRIDGE_MANIFESTS)
def test_bridge_declaration_has_no_unexpanded_placeholder(manifest: str) -> None:
    """No field may carry a variable the client is expected to expand.

    ``cwd: "${PLUGIN_ROOT}"`` shipped for long enough to break every
    non-Bernstein client: the literal string is not a directory, so the
    server process failed to start and the bridge showed as disconnected
    with no error the operator could see.
    """
    for server_name, entry in _servers(manifest).items():
        for field, value in entry.items():
            values = value if isinstance(value, list) else [value]
            for item in values:
                if not isinstance(item, str):
                    continue
                assert not _PLACEHOLDER.search(item), (
                    f"{manifest}: server {server_name!r} field {field!r} carries "
                    f"an unexpanded placeholder {item!r}; MCP clients pass it through verbatim"
                )


@pytest.mark.parametrize("manifest", BRIDGE_MANIFESTS)
def test_bridge_declaration_does_not_require_a_build_tool(manifest: str) -> None:
    """The bridge must start from a plain install, with no build tool present."""
    for server_name, entry in _servers(manifest).items():
        command = entry.get("command")
        assert command not in _BUILD_TOOLS, (
            f"{manifest}: server {server_name!r} starts via {command!r}, which a plain "
            f"`pip install bernstein` does not provide; use the console script"
        )


def test_declared_bridge_answers_the_mcp_handshake() -> None:
    """The declared command really speaks MCP over stdio.

    A round trip against the actual argv, rather than a shape assertion:
    the previous breakage was a command that existed and exited 0 without
    ever answering ``initialize``.
    """
    entry = _servers(".mcp.json")["bernstein"]
    argv = [entry["command"], *entry.get("args", [])]
    if shutil.which(argv[0]) is None:
        pytest.skip(f"{argv[0]} is not on PATH in this environment")

    request = (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "0"},
                },
            }
        )
        + "\n"
    )
    proc = subprocess.run(
        argv,
        input=request,
        capture_output=True,
        text=True,
        timeout=90,
        cwd=REPO,
        check=False,
    )
    assert '"result"' in proc.stdout, (
        f"declared bridge argv {argv} did not answer initialize over stdio; "
        f"rc={proc.returncode} stdout={proc.stdout[:400]!r} stderr={proc.stderr[:400]!r}"
    )


def test_projected_manifest_matches_the_source_declaration() -> None:
    """``mcp.json`` stays the projection of ``.mcp.json`` it claims to be."""
    source = _servers(".mcp.json")
    projected = _servers("mcp.json")
    assert set(source) == set(projected)
    for name, entry in source.items():
        expected = dict(entry)
        expected.setdefault("type", "stdio")
        assert projected[name] == expected, (
            f"mcp.json drifted from .mcp.json for server {name!r}; "
            f"regenerate with scripts/gen_distribution_manifests.py"
        )


def test_generator_reports_no_drift() -> None:
    """The committed manifests match what the generator produces."""
    result = subprocess.run(
        [sys.executable, "scripts/gen_distribution_manifests.py", "--check"],
        capture_output=True,
        text=True,
        cwd=REPO,
        check=False,
    )
    assert result.returncode == 0, f"manifest drift: {result.stdout}\n{result.stderr}"
