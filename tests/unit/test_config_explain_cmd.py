"""`bernstein config explain`, and the one definition of the precedence order.

"Why is this value what it is" had no command: an operator read every config
file by hand and guessed at precedence. `config list` answers it for a human
but names no file and emits no machine-readable shape, so a CI check that
wanted to assert "this came from the project layer, not the environment" had
nothing to read (#5110).

The order itself is the other half. It was stated twice in prose in
`core/config/home.py` and both copies had drifted from the resolver -- the
module header said four layers, `resolve_config` said five, and the code built
six. `CONFIG_PRECEDENCE` is now the only statement of it, and the tests below
fail if the resolver, the command, or the `ConfigSource` vocabulary parts ways
with it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, get_args

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.workspace_cmd import config_group
from bernstein.core.config.home import (
    CONFIG_LAYER_DESCRIPTIONS,
    CONFIG_PRECEDENCE,
    BernsteinHome,
    ConfigSource,
    resolve_config,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A project whose `.sdd/config.yaml` sets two keys."""
    sdd = tmp_path / ".sdd"
    sdd.mkdir()
    (sdd / "config.yaml").write_text("cli: codex\nmax_agents: 3\n", encoding="utf-8")
    return tmp_path


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep the developer's real ~/.bernstein out of the resolution."""
    monkeypatch.setenv("BERNSTEIN_HOME", str(tmp_path / "home"))
    for leaked in ("BERNSTEIN_CLI", "BERNSTEIN_EFFORT", "BERNSTEIN_MAX_AGENTS", "BERNSTEIN_MODEL"):
        monkeypatch.delenv(leaked, raising=False)
    yield


# ---------------------------------------------------------------------------
# The order, defined once
# ---------------------------------------------------------------------------


def test_config_precedence_is_defined_once() -> None:
    """Every layer the vocabulary admits has a place in the order.

    Adding a source to `ConfigSource` without placing it in
    `CONFIG_PRECEDENCE` would leave the resolver building a chain the command
    cannot order -- which is how `context` came to exist in the code while two
    docstrings still described the five-layer world.
    """
    assert set(CONFIG_PRECEDENCE) == set(get_args(ConfigSource))
    assert len(CONFIG_PRECEDENCE) == len(set(CONFIG_PRECEDENCE))


def test_every_layer_is_described_for_an_operator() -> None:
    """A layer that cannot be explained is not an answer to "where did this come from"."""
    assert set(CONFIG_LAYER_DESCRIPTIONS) == set(CONFIG_PRECEDENCE)
    assert all(CONFIG_LAYER_DESCRIPTIONS[layer].strip() for layer in CONFIG_PRECEDENCE)


def test_the_resolver_builds_its_chain_in_that_order(project: Path) -> None:
    """The printed order is the resolver's order, not a second opinion."""
    rank = {layer: index for index, layer in enumerate(CONFIG_PRECEDENCE)}
    for key in ("cli", "max_agents", "budget"):
        chain = resolve_config(key, home=BernsteinHome.default(), project_dir=project)["source_chain"]
        ranks = [rank[layer["source"]] for layer in chain]
        assert ranks == sorted(ranks), f"{key} chain out of precedence order: {ranks}"


def test_the_winning_source_is_the_head_of_the_chain(project: Path) -> None:
    """`source` and `source_chain[0]` cannot disagree about who won."""
    resolution = resolve_config("cli", home=BernsteinHome.default(), project_dir=project)
    assert resolution["source_chain"][0]["source"] == resolution["source"]


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


def _explain(*args: str) -> tuple[int, str]:
    result = CliRunner().invoke(config_group, ["explain", *args])
    return result.exit_code, result.output


def test_explain_names_the_layer_a_value_came_from(project: Path) -> None:
    """The whole point: a project-set value is reported as project, not default."""
    code, output = _explain("cli", "--project-dir", str(project), "--json")
    assert code == 0
    payload = json.loads(output)
    setting = payload["settings"][0]
    assert setting["key"] == "cli"
    assert setting["value"] == "codex"
    assert setting["layer"] == "project"
    assert setting["path"] is not None
    assert setting["path"].endswith("config.yaml")


def test_explain_reports_the_file_each_layer_was_read_from(project: Path) -> None:
    """`config list` answers the layer question but never names the file."""
    _, output = _explain("max_agents", "--project-dir", str(project), "--json")
    chain = json.loads(output)["settings"][0]["chain"]
    project_layer = next(layer for layer in chain if layer["source"] == "project")
    assert Path(project_layer["path"]).is_file()


def test_a_session_override_outranks_the_project_file(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The direction an operator most often needs confirmed."""
    monkeypatch.setenv("BERNSTEIN_CLI", "gemini")
    _, output = _explain("cli", "--project-dir", str(project), "--json")
    setting = json.loads(output)["settings"][0]
    assert setting["value"] == "gemini"
    assert setting["layer"] == "session"


def test_the_json_payload_carries_the_order_it_resolved_by(project: Path) -> None:
    """A caller reading the report does not have to hardcode the precedence."""
    _, output = _explain("--project-dir", str(project), "--json")
    assert json.loads(output)["precedence"] == list(CONFIG_PRECEDENCE)


def test_with_no_key_every_setting_is_reported(project: Path) -> None:
    """The survey form, which is what an operator opens first."""
    _, output = _explain("--project-dir", str(project), "--json")
    keys = {setting["key"] for setting in json.loads(output)["settings"]}
    assert {"cli", "max_agents", "budget", "model", "effort"} <= keys


def test_an_unknown_key_exits_non_zero_and_lists_the_known_ones(project: Path) -> None:
    """A typo must not read as "this setting resolves to nothing"."""
    code, output = _explain("clii", "--project-dir", str(project))
    assert code == 1
    assert "unknown config key" in output
    assert "cli" in output


def test_the_table_states_the_precedence_it_used(project: Path) -> None:
    """The human output answers "in what order" without a second command."""
    _, output = _explain("cli", "--project-dir", str(project))
    condensed = " ".join(output.split())
    assert "seed > session > project" in condensed


def test_a_redacted_value_is_what_gets_printed(project: Path) -> None:
    """A resolution report is what an operator pastes into an issue.

    The command prints `redacted_value`, never the raw one, so a secret
    resolved from any layer is not the thing that leaks.
    """
    _, output = _explain("--project-dir", str(project), "--json")
    for setting in json.loads(output)["settings"]:
        for layer in setting["chain"]:
            resolution = resolve_config(setting["key"], home=BernsteinHome.default(), project_dir=project)
            match = next(
                (entry for entry in resolution["source_chain"] if entry["source"] == layer["source"]),
                None,
            )
            assert match is not None
            assert layer["value"] == match["redacted_value"]
