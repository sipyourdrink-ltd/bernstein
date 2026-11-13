"""Every ``bernstein`` command a recovery hint tells the operator to run exists.

Two hints named commands the CLI rejects: the unknown-error hint said
``bernstein run --verbose`` (``--verbose`` is a group option, so ``run``
refuses it), and the air-gap doctor's MCP row said ``bernstein mcp catalog
remove`` (the subcommand is ``uninstall``). The model-unreachable hint told
the operator that ``BERNSTEIN_OFFLINE=1`` switches to the mock adapter; it
only makes the doctor skip its network probes.

Each hint's command is resolved against the real click tree, so a renamed
command or flag turns this red.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import click
import pytest

from bernstein.cli.main import cli
from bernstein.core.distribution.doctor_airgap import check_mcp_catalog_all_off
from bernstein.core.errors import ErrorCategory
from bernstein.core.errors.hints import _hint_body  # type: ignore[attr-defined]

_CONTEXT = {
    "adapter": "claude",
    "provider": "anthropic",
    "port": 8052,
    "path": "/tmp/wt",
    "timeout_seconds": 30,
}


def _unknown_parts(command: str) -> list[str]:
    """Words and long options of a ``bernstein ...`` command that the CLI does not have."""
    tokens = shlex.split(command)
    tokens = tokens[tokens.index("bernstein") + 1 :]
    ctx = click.Context(cli)
    node: click.Command = cli
    unknown: list[str] = []
    for token in tokens:
        if token.startswith("--"):
            options = {opt for param in node.params for opt in (*param.opts, *param.secondary_opts)}
            if token.split("=")[0] not in options:
                unknown.append(token)
            continue
        if token.startswith("<") or token.startswith("-"):
            continue
        if isinstance(node, click.Group):
            child = node.get_command(ctx, token)
            if child is None:
                unknown.append(token)
                break
            node = child
    return unknown


def _bernstein_commands(text: str) -> list[str]:
    return [text[text.index("bernstein") :]] if "bernstein " in text else []


@pytest.mark.parametrize("category", list(ErrorCategory), ids=lambda c: c.name)
def test_every_hint_command_resolves(category: ErrorCategory) -> None:
    _prose, command = _hint_body(category, dict(_CONTEXT))  # type: ignore[arg-type]
    for candidate in _bernstein_commands(command):
        assert _unknown_parts(candidate) == [], f"{category.name}: {candidate!r}"


def test_airgap_mcp_fix_names_a_real_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = tmp_path / "bernstein" / "mcp.json"
    cfg.parent.mkdir(parents=True)
    monkeypatch.setattr(
        "bernstein.core.protocols.mcp_catalog.user_config.default_user_config_path",
        lambda: cfg,
    )
    monkeypatch.setattr(
        "bernstein.core.protocols.mcp_catalog.user_config.list_installed",
        lambda _path: [type("Entry", (), {"id": "evil"})()],
    )
    cfg.write_text("{}", encoding="utf-8")

    row = check_mcp_catalog_all_off()

    assert row.fix is not None
    command = row.fix.split(" for each")[0]
    assert _unknown_parts(command) == [], row.fix
