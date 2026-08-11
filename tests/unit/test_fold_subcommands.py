"""Unit tests for #3140: folding quickstart, init-wizard, validate and routine
into the commands that already own their domain.

Two rules this file exists to enforce, both learned the hard way here:

*Assert the forwarding, not the help text.* The forwarding body is the entire
content of the change, and a help-text assertion does not see it: with every
``ctx.invoke`` in the four folds deleted, a suite that only greps ``--help``
stays green. Worse, ``--flask-todo`` appears in ``demo``'s own docstring, so
``"--flask-todo" in result.output`` passes with the option itself removed.
Registration is therefore asserted against ``Command.params`` and behaviour
against the target callback.

*Never let a test run a real orchestration.* The scenario behind
``quickstart``/``demo --flask-todo`` bootstraps a task server on a fixed port,
spawns agents, and picks up an installed CLI adapter - a live, billable run
inside the unit suite that also leaks processes past the test. Its callback is
replaced in every test that reaches it.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import click
import pytest
from click.testing import CliRunner

from bernstein.cli.commands.init_wizard_cmd import init_wizard_cmd
from bernstein.cli.commands.plan_validate_cmd import validate_plan
from bernstein.cli.commands.quickstart_cmd import quickstart_alias_cmd, quickstart_cmd
from bernstein.cli.commands.routine_cmd import routine_alias_group, routine_group
from bernstein.cli.main import cli, demo, init
from bernstein.cli.utils.aliases import ALIASES, expand_alias

if TYPE_CHECKING:
    from collections.abc import Sequence

_PLAN_YAML = "stages:\n  - name: test\n    steps: []\n"

_REPO_ROOT = Path(__file__).parent.parent.parent
_FLASK_TODO_DOC = _REPO_ROOT / "docs" / "getting-started" / "quickstart-demo.md"


def _capture(monkeypatch: pytest.MonkeyPatch, command: click.Command) -> list[dict[str, object]]:
    """Replace ``command``'s callback and record every invocation's kwargs.

    ``Context.invoke`` reads ``other_cmd.callback`` at call time and fills in
    the target's own defaults for anything the caller omitted, so the recorded
    kwargs are exactly what the target would have run with.
    """
    calls: list[dict[str, object]] = []

    def _record(**kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(command, "callback", _record)
    return calls


def _long_opts(command: click.Command) -> set[str]:
    """Every long option the parser actually accepts for ``command``."""
    return {opt for param in command.params for opt in param.opts if opt.startswith("--")}


def _default_of(command: click.Command, param_name: str) -> object:
    return next(param.default for param in command.params if param.name == param_name)


def _subcommand_names(group: click.Group) -> set[str]:
    return set(group.commands)


def _resolve(path: Sequence[str]) -> click.Command | None:
    """Walk a command path through the real top-level CLI object."""
    node: click.Command | None = cli
    for word in path:
        if not isinstance(node, click.Group):
            return None
        node = node.get_command(click.Context(node), word)
        if node is None:
            return None
    return node


# ---------------------------------------------------------------------------
# quickstart -> demo --flask-todo
# ---------------------------------------------------------------------------


def test_demo_registers_flask_todo_as_a_real_option() -> None:
    """``--flask-todo`` must be a parsed option, not only a docstring mention.

    ``demo``'s help epilog names ``bernstein demo --flask-todo``, so matching
    that string in ``--help`` output passes even with the option deleted.
    """
    assert "--flask-todo" in _long_opts(demo)


def test_demo_flask_todo_forwards_the_operators_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    """The flag must reach the scenario carrying what the operator asked for."""
    calls = _capture(monkeypatch, quickstart_cmd)
    result = CliRunner().invoke(cli, ["demo", "--flask-todo", "--timeout", "7", "--keep"])

    assert result.exit_code == 0, f"{result.output}\n{result.exception!r}"
    assert len(calls) == 1, calls
    assert calls[0]["timeout"] == 7
    assert calls[0]["keep"] is True


def test_demo_flask_todo_exposes_the_scenarios_keep_flag() -> None:
    """``quickstart --keep`` is documented; the fold must not delete it.

    Without ``--keep`` on ``demo`` the migration target cannot preserve the
    project directory at all, so the capability is gone rather than moved.
    """
    assert "--keep" in _long_opts(demo)


def test_demo_flask_todo_inherits_the_scenarios_timeout_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unset ``--timeout`` must not silently shrink the scenario's budget.

    ``demo`` defaults to 120s and the scenario to 300s. Forwarding ``demo``'s
    default halves the completion budget of the command being migrated to.
    """
    demo_default = _default_of(demo, "timeout")
    scenario_default = _default_of(quickstart_cmd, "timeout")
    assert demo_default != scenario_default, "test is meaningless if the two defaults agree"

    calls = _capture(monkeypatch, quickstart_cmd)
    result = CliRunner().invoke(cli, ["demo", "--flask-todo"])

    assert result.exit_code == 0, f"{result.output}\n{result.exception!r}"
    assert calls[0]["timeout"] == scenario_default


def test_demo_flask_todo_stays_on_mock_agents_without_real(monkeypatch: pytest.MonkeyPatch) -> None:
    """``demo`` is mock-by-default; ``--flask-todo`` must not spend money.

    The scenario body resolves ``adapter or detect_available_adapter() or
    "mock"``. Forwarding an unresolved None made a bare ``demo --flask-todo``
    pick up an installed CLI and run billable agents, while plain ``demo``
    stayed free.
    """
    from bernstein.cli import run_confirm

    monkeypatch.setattr(run_confirm, "detect_available_adapter", lambda: "claude")
    calls = _capture(monkeypatch, quickstart_cmd)
    result = CliRunner().invoke(cli, ["demo", "--flask-todo"])

    assert result.exit_code == 0, f"{result.output}\n{result.exception!r}"
    assert calls[0]["adapter"] == "mock"


def test_demo_flask_todo_uses_a_real_adapter_only_behind_real(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--real`` is the one path that resolves an installed CLI."""
    from bernstein.cli import run_confirm

    monkeypatch.setattr(run_confirm, "detect_available_adapter", lambda: "claude")
    calls = _capture(monkeypatch, quickstart_cmd)
    result = CliRunner().invoke(cli, ["demo", "--flask-todo", "--real"])

    assert result.exit_code == 0, f"{result.output}\n{result.exception!r}"
    assert calls[0]["adapter"] == "claude"


def test_demo_flask_todo_real_without_an_adapter_fails_like_demo(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--real`` with nothing installed must fail, not quietly run on mock.

    ``demo --real`` prints ``no_cli_agent_found()`` and exits 1. The scenario
    body instead falls through to ``"mock"``, so an operator who explicitly
    asked for real agents got a mock run reported as a success.
    """
    from bernstein.cli import run_confirm

    monkeypatch.setattr(run_confirm, "detect_available_adapter", lambda: None)
    calls = _capture(monkeypatch, quickstart_cmd)
    result = CliRunner().invoke(cli, ["demo", "--flask-todo", "--real"])

    assert result.exit_code != 0, result.output
    assert calls == [], "the scenario must not run when no adapter can be resolved"


def test_demo_flask_todo_refuses_dry_run_instead_of_ignoring_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--dry-run`` promises no agents are spawned; the scenario has no preview.

    Accepting the combination ran a full live orchestration for an operator who
    asked for a plan preview.
    """
    calls = _capture(monkeypatch, quickstart_cmd)
    result = CliRunner().invoke(cli, ["demo", "--flask-todo", "--dry-run"])

    assert result.exit_code != 0, result.output
    assert "--dry-run" in result.output
    assert calls == [], "the scenario must not run when the combination is refused"


def _doc_section(text: str, heading: str) -> str:
    """Return the body of a single ``## heading`` section of a markdown page."""
    body = text.split(f"\n## {heading}\n", 1)[1]
    return body.split("\n## ", 1)[0]


def test_flask_todo_cost_docs_cover_both_spellings() -> None:
    """The two spellings do not cost the same, so the cost section must say so.

    ``demo --flask-todo`` decides the adapter before the scenario body runs, so
    its ``adapter or detect_available_adapter() or "mock"`` branch is
    unreachable and the run cannot spend money without ``--real``. The retained
    ``quickstart`` spelling has no ``--real`` option at all and still reaches
    that branch, so on a machine with an agent CLI on PATH it spends money with
    no flag asked for. The cost section is the paragraph a first-run reader
    checks before deciding whether the command bills them; written for only one
    of the two spellings it misleads whoever is on the other.
    """
    assert "--real" in _long_opts(demo)
    assert "--real" not in _long_opts(quickstart_alias_cmd), (
        "the deprecated spelling gained --real; the cost section needs rewriting to match"
    )

    cost = _doc_section(_FLASK_TODO_DOC.read_text(encoding="utf-8"), "Cost")

    assert "--real" in cost, "the cost section must name the flag that decides whether money is spent"
    assert "bernstein quickstart" in cost, (
        "the retained spelling bills without a flag; the cost section must not describe only demo --flask-todo"
    )


def test_deprecated_quickstart_warns_and_forwards(monkeypatch: pytest.MonkeyPatch) -> None:
    """The retained top-level name warns on stderr and still does its work."""
    calls = _capture(monkeypatch, quickstart_cmd)
    result = CliRunner().invoke(cli, ["quickstart", "--keep", "--timeout", "9"])

    assert "WARNING: 'bernstein quickstart' is deprecated" in result.stderr
    assert calls == [{"keep": True, "timeout": 9, "adapter": None}], calls


# ---------------------------------------------------------------------------
# init-wizard -> init --wizard
# ---------------------------------------------------------------------------


def test_init_registers_wizard_as_a_real_option() -> None:
    assert "--wizard" in _long_opts(init)


def test_init_wizard_forwards_to_the_wizard(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = _capture(monkeypatch, init_wizard_cmd)
    result = CliRunner().invoke(cli, ["init", "--wizard", "--dir", str(tmp_path)])

    assert result.exit_code == 0, f"{result.output}\n{result.exception!r}"
    assert calls == [{"target_dir": str(tmp_path), "non_interactive": False}], calls


def test_init_wizard_can_still_run_non_interactively(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``init-wizard --non-interactive`` must survive the fold.

    The wizard's unattended path had no equivalent on ``init``, so the flag
    conversion removed a capability instead of moving it.
    """
    assert "--non-interactive" in _long_opts(init)

    calls = _capture(monkeypatch, init_wizard_cmd)
    result = CliRunner().invoke(cli, ["init", "--wizard", "--non-interactive", "--dir", str(tmp_path)])

    assert result.exit_code == 0, f"{result.output}\n{result.exception!r}"
    assert calls == [{"target_dir": str(tmp_path), "non_interactive": True}], calls


@pytest.mark.parametrize("flag", ["--add-badge", "--remote"])
def test_init_wizard_refuses_flags_it_cannot_honour(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, flag: str) -> None:
    """The wizard never reaches the badge or remote-container code.

    Accepting these flags alongside ``--wizard`` dropped them silently, which
    reads to the operator as the flag being broken.
    """
    calls = _capture(monkeypatch, init_wizard_cmd)
    result = CliRunner().invoke(cli, ["init", "--wizard", flag, "--dir", str(tmp_path)])

    assert result.exit_code != 0, result.output
    assert flag in result.output
    assert calls == []


def test_deprecated_init_wizard_warns_and_forwards(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = _capture(monkeypatch, init_wizard_cmd)
    result = CliRunner().invoke(cli, ["init-wizard", "--non-interactive", "--dir", str(tmp_path)])

    assert "WARNING: 'bernstein init-wizard' is deprecated" in result.stderr
    assert calls == [{"target_dir": str(tmp_path), "non_interactive": True}], calls


# ---------------------------------------------------------------------------
# the `i` shortcut
# ---------------------------------------------------------------------------


def test_alias_i_expands_onto_the_init_wizard_path() -> None:
    """``i`` pointed at ``init-wizard``; after the fold it must still open it.

    Repointing it at bare ``init`` keeps the shortcut resolving but silently
    changes what it does: the interactive setup it has always opened becomes
    non-interactive scaffolding.
    """
    assert expand_alias("i") == ["init", "--wizard"]
    assert ALIASES["i"].split()[0] == "init"


def test_alias_i_actually_runs_the_wizard(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Resolution is not enough: driving ``bernstein i`` must reach the wizard."""
    calls = _capture(monkeypatch, init_wizard_cmd)
    result = CliRunner().invoke(cli, ["i", "--non-interactive", "--dir", str(tmp_path)])

    assert result.exit_code == 0, f"{result.output}\n{result.exception!r}"
    assert calls == [{"target_dir": str(tmp_path), "non_interactive": True}], calls


def test_single_word_aliases_still_resolve() -> None:
    """Alias values carrying flags must not break the ordinary one-word case."""
    result = CliRunner().invoke(cli, ["s", "--help"])
    assert result.exit_code == 0, f"{result.output}\n{result.exception!r}"
    assert expand_alias("s") == ["status"]
    assert expand_alias("zzz") is None


# ---------------------------------------------------------------------------
# validate -> plan validate
# ---------------------------------------------------------------------------


def test_plan_validate_runs_the_validator(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    plan = tmp_path / "plan.yaml"
    plan.write_text(_PLAN_YAML)

    calls = _capture(monkeypatch, validate_plan)
    result = CliRunner().invoke(cli, ["plan", "validate", str(plan)])

    assert result.exit_code == 0, f"{result.output}\n{result.exception!r}"
    assert calls == [{"plan_file": plan}], calls


def test_deprecated_validate_warns_and_forwards(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    plan = tmp_path / "plan.yaml"
    plan.write_text(_PLAN_YAML)

    calls = _capture(monkeypatch, validate_plan)
    result = CliRunner().invoke(cli, ["validate", str(plan)])

    assert "WARNING: 'bernstein validate' is deprecated" in result.stderr
    assert calls == [{"plan_file": plan}], calls


# ---------------------------------------------------------------------------
# routine -> schedule routine
# ---------------------------------------------------------------------------


def test_schedule_routine_carries_every_routine_subcommand() -> None:
    """``export`` and ``register`` are the two the move was required to keep.

    Asserting only that ``schedule routine --help`` exits 0 passes with an
    empty group registered under the name.
    """
    folded = _resolve(["schedule", "routine"])
    assert isinstance(folded, click.Group)
    assert _subcommand_names(folded) == _subcommand_names(routine_group)
    assert {"export", "register"} <= _subcommand_names(folded)


def test_deprecated_routine_group_keeps_its_subcommands_and_warns() -> None:
    """The retained top-level group must expose the same surface, with a warning."""
    assert _subcommand_names(routine_alias_group) == _subcommand_names(routine_group)

    result = CliRunner().invoke(cli, ["routine", "bindings"])
    assert "WARNING: 'bernstein routine' is deprecated" in result.stderr


def test_folded_routine_path_does_not_warn() -> None:
    """The migration target is the supported spelling: it must stay quiet."""
    result = CliRunner().invoke(cli, ["schedule", "routine", "bindings"])
    assert "deprecated" not in result.stderr


# ---------------------------------------------------------------------------
# curated first-run surface
# ---------------------------------------------------------------------------


def test_curated_help_does_not_advertise_the_deprecated_entrypoint() -> None:
    """The duplicate first-run entry in the curated panel is what #3140 names.

    Leaving ``quickstart`` listed there next to ``demo`` keeps presenting two
    indistinguishable first-run commands, which is the condition the fold is
    meant to remove.
    """
    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0, result.output
    assert "demo --flask-todo" in result.output
    assert "quickstart" not in result.output
