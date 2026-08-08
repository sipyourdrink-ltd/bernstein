"""Unit tests for HermesAdapter spawn/name.

``hermes`` exposes no top-level positional parameter. Its only positional slot
is the subcommand, so a bare prompt is parsed as a command name and the process
exits 2 before a model is contacted - the agent never runs and the worktree
diff is empty. The prompt therefore has to arrive attached to the one-shot
flag, and the tests below are written to catch a command line that is *wrong*
rather than merely *changed*:

* :meth:`test_no_argument_lands_in_the_subcommand_slot` asserts the property
  that upstream parser imposes, not the string this implementation happens to
  emit. A rewrite that reintroduces a bare prompt fails it whatever the
  wording;
* the dash-prefixed prompt case pins the reason for ``--oneshot=<prompt>``
  over ``-z <prompt>``, which is otherwise an invisible detail one refactor
  away from regressing;
* stdin and the empty-prompt guard cover the same hazard from both sides -
  one-shot dispatches on the prompt being truthy, so a blank prompt reaches
  the interactive path and waits on stdin for the whole timeout.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters.hermes import HermesAdapter
from tests.unit._adapter_test_helpers import inner_cmd, make_popen_mock

if TYPE_CHECKING:
    from pathlib import Path


pytestmark = pytest.mark.usefixtures("no_watchdog_threads")


def _spawn(adapter: HermesAdapter, tmp_path: Path, prompt: str, session_id: str):
    """Spawn against a mocked ``Popen`` and hand back the mock."""
    proc_mock = make_popen_mock(pid=900)
    with patch("bernstein.adapters.hermes.subprocess.Popen", return_value=proc_mock) as popen:
        adapter.spawn(
            prompt=prompt,
            workdir=tmp_path,
            model_config=ModelConfig(model="sonnet", effort="high"),
            session_id=session_id,
        )
    return popen


class TestHermesAdapterSpawn:
    def test_spawn_passes_the_prompt_through_the_oneshot_flag(self, tmp_path: Path) -> None:
        popen = _spawn(HermesAdapter(), tmp_path, "ship the feature", "hermes-s1")
        assert inner_cmd(popen.call_args.args[0]) == ["hermes", "--oneshot=ship the feature"]

    def test_the_prompt_is_never_its_own_argument(self, tmp_path: Path) -> None:
        """Asserted as a property, so a rewrite cannot reintroduce the defect.

        Two distinct failures are ruled out by the same rule. A bare
        ``["hermes", prompt]`` offers the prompt to the only positional slot
        there is - the subcommand - and the process exits 2 before reaching a
        model, which is the form that shipped. And a prompt that begins with a
        dash is read as flags when it stands alone.

        ``["hermes", "-z", prompt]`` avoids the first failure, because the flag
        consumes the value before the subparser sees it, but not the second.
        Keeping the prompt inside a single ``--oneshot=<prompt>`` token avoids
        both, and that is what this asserts - not the exact spelling above it.
        """
        prompt = "ship the feature"
        popen = _spawn(HermesAdapter(), tmp_path, prompt, "hermes-slot")
        binary, *arguments = inner_cmd(popen.call_args.args[0])

        assert binary == "hermes"
        assert prompt not in arguments, (
            "the prompt is a standalone argument here. On its own it is offered "
            "to the subcommand slot, and a dash-leading prompt is read as flags. "
            "It has to stay inside one token with the flag that consumes it."
        )
        assert any(prompt in argument for argument in arguments), "the prompt must still be passed"

    def test_a_dash_leading_prompt_stays_attached_to_its_flag(self, tmp_path: Path) -> None:
        """``-z <prompt>`` would read this prompt as flags; ``--oneshot=`` cannot."""
        prompt = "--help me remove the -rf guard in scripts/clean.sh"
        popen = _spawn(HermesAdapter(), tmp_path, prompt, "hermes-dash")
        arguments = inner_cmd(popen.call_args.args[0])[1:]

        assert arguments == [f"--oneshot={prompt}"]
        assert len(arguments) == 1, "the prompt must not be split into separate argv entries"

    def test_spawn_closes_stdin(self, tmp_path: Path) -> None:
        """An inherited stdin lets any interactive path block until timeout."""
        import subprocess

        popen = _spawn(HermesAdapter(), tmp_path, "ship the feature", "hermes-stdin")
        assert popen.call_args.kwargs["stdin"] is subprocess.DEVNULL

    def test_empty_prompt_is_refused_before_spawning(self, tmp_path: Path) -> None:
        """One-shot dispatches on truthiness, so a blank prompt goes interactive."""
        adapter = HermesAdapter()
        with patch("bernstein.adapters.hermes.subprocess.Popen") as popen:
            for blank in ("", "   ", "\n\t"):
                with pytest.raises(ValueError, match="non-empty prompt"):
                    adapter.spawn(
                        prompt=blank,
                        workdir=tmp_path,
                        model_config=ModelConfig(model="sonnet", effort="high"),
                        session_id="hermes-blank",
                    )
        popen.assert_not_called()

    def test_provider_credentials_reach_the_agent(self, tmp_path: Path) -> None:
        """Names come from what the agent documents, not from the vendor's name.

        A credential missing here is invisible to the operator: the agent
        starts, fails to authenticate, and the run reads as a model problem.
        """
        with patch("bernstein.adapters.hermes.build_filtered_env", return_value={}) as build_env:
            _spawn(HermesAdapter(), tmp_path, "ship the feature", "hermes-env")

        forwarded = set(build_env.call_args.args[0])
        assert {"OPENROUTER_API_KEY", "NOUS_API_KEY"} <= forwarded, (
            "both are documented provider credentials - OPENROUTER_API_KEY in "
            "the agent's .env.example, NOUS_API_KEY for its `nous-api` provider "
            "in cli-config.yaml.example. Dropping either silently deauthenticates "
            "an operator who configured it"
        )
        assert "HERMES_API_KEY" not in forwarded, (
            "this name appears in neither documented config, so nothing reads it; "
            "forwarding it advertises a credential path that does not exist"
        )
        assert "GITHUB_TOKEN" not in forwarded, (
            "forwarding a repository-scoped token to an agent whose approvals are "
            "auto-bypassed is a separate decision from forwarding a model "
            "credential, and is not made implicitly here"
        )

    def test_network_policy_is_enforced_before_spawning(self, tmp_path: Path) -> None:
        """This adapter forwards provider credentials for a dozen hosted APIs.

        Under a restricted or air-gapped policy that has to be checked before
        the process starts, not after it has already reached out.
        """
        adapter = HermesAdapter()
        with (
            patch.object(HermesAdapter, "enforce_network_policy", side_effect=RuntimeError("blocked")),
            patch("bernstein.adapters.hermes.subprocess.Popen") as popen,
            pytest.raises(RuntimeError, match="blocked"),
        ):
            adapter.spawn(
                prompt="ship the feature",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="hermes-net",
            )
        popen.assert_not_called()

    def test_spawn_translates_missing_cli(self, tmp_path: Path) -> None:
        adapter = HermesAdapter()
        with (
            patch(
                "bernstein.adapters.hermes.subprocess.Popen",
                side_effect=FileNotFoundError("No such file"),
            ),
            pytest.raises(RuntimeError, match=r"hermes not found.*NousResearch/hermes-agent"),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="hermes-missing",
            )


class TestHermesAdapterName:
    def test_name(self) -> None:
        assert HermesAdapter().name() == "Hermes Agent"


class TestHermesDeclaredStrategy:
    def test_the_declared_permission_axis_matches_the_mode_we_drive(self) -> None:
        """`unsupported` here reads as "cannot be driven unattended" - it can."""
        from bernstein.adapters._contract import STRATEGY_MATRIX, DangerousModeStrategy

        assert STRATEGY_MATRIX["hermes"].dangerous_mode is DangerousModeStrategy.ALWAYS_ON, (
            "one-shot mode auto-bypasses approvals with no flag to opt out, so "
            "the axis is always-on. Declaring it unsupported understates what an "
            "operator authorises by selecting this adapter."
        )
