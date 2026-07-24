"""Adapter selection must track the live registry, not stale allowlists.

Regression cover for issue #2781. The seed ``cli:`` allowlist and every
``--cli`` ``click.Choice`` derive from
:func:`bernstein.adapters.registry.selectable_adapter_names`, so a newly
registered adapter is selectable on every surface at once and the four
selection surfaces (seed allowlist, main-group ``--cli``, ``run`` ``--cli``,
``session replay`` ``--cli``) cannot drift apart from the registry.
"""

from __future__ import annotations

from pathlib import Path

import click
import pytest
from bernstein.core.seed import SeedError, parse_seed, valid_cli_selections

from bernstein.adapters.registry import iter_adapter_specs, selectable_adapter_names

_AUTO = "auto"

# Names the pre-#2781 hardcoded lists accepted; all must stay selectable so the
# fix only widens the allowlist and never drops a previously valid selection.
_HISTORICALLY_ALLOWED = frozenset({"claude", "codex", "gemini", "qwen", "opencode", "aider", _AUTO})


def _registry_names() -> set[str]:
    """Every registry adapter name, the source ``bernstein adapters list`` uses."""
    return {name for name, _ in iter_adapter_specs()}


def _cli_choice_values(command: click.Command) -> set[str]:
    """Return the ``--cli`` option's choice set for a Click command."""
    for param in command.params:
        if "--cli" in getattr(param, "opts", ()):
            param_type = param.type
            assert isinstance(param_type, click.Choice), f"--cli on {command.name!r} is not a click.Choice"
            return {str(choice) for choice in param_type.choices}
    raise AssertionError(f"command {command.name!r} has no --cli option")


def test_selectable_names_are_registry_minus_stubs() -> None:
    """Selectable set is the whole registry except the non-agent ``mock`` stub."""
    selectable = selectable_adapter_names()
    registry = _registry_names()
    assert "mock" not in selectable, "the test-only mock stub must not be selectable"
    assert selectable == registry - {"mock"}


def test_historically_allowed_names_stay_selectable() -> None:
    """The pre-fix allowlist is a subset of the new allowlist (no regression)."""
    assert (selectable_adapter_names() | {_AUTO}) >= _HISTORICALLY_ALLOWED


def test_seed_allowlist_matches_registry() -> None:
    """The seed ``cli:`` allowlist is exactly the selectable registry plus ``auto``."""
    assert valid_cli_selections() == selectable_adapter_names() | {_AUTO}


def test_every_cli_choice_matches_the_seed_allowlist() -> None:
    """The seed allowlist and all three ``--cli`` choices agree with each other."""
    from bernstein.cli.commands.session_cmd import session_group
    from bernstein.cli.main import cli

    expected = valid_cli_selections()
    run_command = cli.commands["run"]
    replay_command = session_group.commands["replay"]

    assert _cli_choice_values(cli) == expected, "main-group --cli drifted from the registry"
    assert _cli_choice_values(run_command) == expected, "run --cli drifted from the registry"
    assert _cli_choice_values(replay_command) == expected, "session replay --cli drifted from the registry"


@pytest.mark.parametrize("cli_name", ["clm", "agy", "kimi", "droid"])
def test_seed_accepts_registered_adapter(tmp_path: Path, cli_name: str) -> None:
    """A registry-backed adapter parses via ``cli:`` (the issue #2781 repro)."""
    seed = tmp_path / "bernstein.yaml"
    seed.write_text(f'goal: "x"\ncli: {cli_name}\nmodel: m\n')
    config = parse_seed(seed)
    assert config.cli == cli_name


def test_seed_still_rejects_unregistered_adapter(tmp_path: Path) -> None:
    """A name that is not a registered adapter is still a parse error."""
    seed = tmp_path / "bernstein.yaml"
    seed.write_text('goal: "x"\ncli: chatgpt\n')
    with pytest.raises(SeedError, match="cli must be one of"):
        parse_seed(seed)


def test_seed_rejects_removed_adapter_with_replacement_guidance(tmp_path: Path) -> None:
    """A config still pinning a removed adapter is told what to use instead.

    Removed names are absent from the allowlist, so the default error would
    be an alphabetical dump of every valid name. That is a worse answer than
    naming the supported path directly (issue #2970).
    """
    seed = tmp_path / "bernstein.yaml"
    seed.write_text('goal: "x"\ncli: cloudflare\n')
    with pytest.raises(SeedError) as excinfo:
        parse_seed(seed)
    msg = str(excinfo.value)
    assert "has been removed" in msg
    assert "codex_cloudflare" in msg
    assert "cli must be one of" not in msg
