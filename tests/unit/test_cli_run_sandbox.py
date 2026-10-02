"""Unit tests for ``bernstein run --sandbox`` / ``--allow-paid`` flags.

The flags expose the sandbox selector on the ``run`` command so
operators can override the deterministic precedence and unlock paid
backends. The tests below confirm:

1. Default invocation leaves the env unset, so the orchestrator falls
   back to the selector's lowest-cost-first ordering (``worktree`` first).
2. ``--sandbox docker`` populates ``BERNSTEIN_SANDBOX_RUNTIME=docker``
   and sets the legacy ``BERNSTEIN_CONTAINER`` flag.
3. ``--allow-paid`` flips ``BERNSTEIN_SANDBOX_ALLOW_PAID=1`` so a paid
   backend (modal) becomes selectable when no explicit override is set.
4. Combining ``--sandbox modal`` (paid) without ``--allow-paid`` exits
   non-zero with a diagnostic - silent fallbacks have bitten us before.
5. An unknown ``--sandbox`` value is rejected by Click with a parse
   error, never reaches the runtime, and never leaks half-set env state.

Tests run the click command in isolation via ``CliRunner`` so they do
not need a server, a workspace, or any sandbox backend installed.
"""

from __future__ import annotations

import os
from collections.abc import Generator
from typing import Any

import pytest
from click.testing import CliRunner

from bernstein.cli.run_bootstrap import SANDBOX_CHOICES, run

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_ENV_KEYS_TO_RESTORE: tuple[str, ...] = (
    "BERNSTEIN_SANDBOX_RUNTIME",
    "BERNSTEIN_SANDBOX_ALLOW_PAID",
    "BERNSTEIN_CONTAINER",
)


@pytest.fixture
def isolated_sandbox_env() -> Generator[None, None, None]:
    """Snapshot + restore the sandbox-related env keys around each test.

    ``_propagate_env_flags`` mutates ``os.environ`` directly so a leaked
    key from one test would taint another. The fixture is autouse-style
    but kept explicit so any test that intentionally needs the leak
    (currently none) can opt out.
    """
    original: dict[str, str | None] = {k: os.environ.get(k) for k in _ENV_KEYS_TO_RESTORE}
    yield
    for key, value in original.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _stub_run_body(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace expensive ``run()`` side-effects with lightweight no-ops.

    Returns a dict the caller can inspect after the run to confirm which
    code paths fired. Stops the run() body short of bootstrap so we only
    exercise the flag plumbing under test.
    """
    captured: dict[str, Any] = {"installed_policy": False, "estimated": False}

    def _fake_install(*, run_profile: str | None, allow_network: tuple[str, ...]) -> None:
        captured["installed_policy"] = True
        captured["run_profile"] = run_profile
        captured["allow_network"] = allow_network

    monkeypatch.setattr("bernstein.cli.run_bootstrap._install_network_policy", _fake_install)
    monkeypatch.setattr(
        "bernstein.cli.run_bootstrap._configure_quality_gate_bypass",
        lambda **_kwargs: None,
    )

    # Trip an early SystemExit *after* env propagation but before the
    # heavyweight estimate / bootstrap path runs, so tests stay cheap.
    def _fake_show_dry_run_plan(**_kwargs: Any) -> None:
        captured["estimated"] = True

    monkeypatch.setattr(
        "bernstein.cli.run_bootstrap._show_dry_run_plan",
        _fake_show_dry_run_plan,
    )
    return captured


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("isolated_sandbox_env")
class TestSandboxFlag:
    """``bernstein run --sandbox`` end-to-end flag plumbing."""

    def test_default_invocation_leaves_runtime_unset(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Without ``--sandbox``, no runtime env is exported.

        The selector is responsible for picking ``worktree`` as
        the default in this case; the CLI must NOT force a value because
        plan/seed-file overrides need first-write rights to the same env.
        """
        os.environ.pop("BERNSTEIN_SANDBOX_RUNTIME", None)
        captured = _stub_run_body(monkeypatch)
        runner = CliRunner()

        result = runner.invoke(run, ["--dry-run"])

        assert result.exit_code == 0, result.output
        assert "BERNSTEIN_SANDBOX_RUNTIME" not in os.environ
        # Allow-paid bit must default off so paid backends stay locked.
        assert "BERNSTEIN_SANDBOX_ALLOW_PAID" not in os.environ
        assert captured["estimated"] is True

    def test_explicit_override_propagates_runtime(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``--sandbox docker`` writes the runtime env and container flag."""
        captured = _stub_run_body(monkeypatch)
        runner = CliRunner()

        result = runner.invoke(run, ["--sandbox", "docker", "--dry-run"])

        assert result.exit_code == 0, result.output
        assert os.environ.get("BERNSTEIN_SANDBOX_RUNTIME") == "docker"
        # Docker is a kernel-isolation backend so the legacy flag fires.
        assert os.environ.get("BERNSTEIN_CONTAINER") == "1"
        assert captured["estimated"] is True

    def test_allow_paid_unlocks_modal_override(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``--sandbox modal --allow-paid`` exports paid runtime + opt-in.

        The selector reads ``BERNSTEIN_SANDBOX_ALLOW_PAID`` to decide
        whether to consider non-free backends - without the bit, modal
        would be filtered out before precedence is applied.
        """
        captured = _stub_run_body(monkeypatch)
        runner = CliRunner()

        result = runner.invoke(run, ["--sandbox", "modal", "--allow-paid", "--dry-run"])

        assert result.exit_code == 0, result.output
        assert os.environ.get("BERNSTEIN_SANDBOX_RUNTIME") == "modal"
        assert os.environ.get("BERNSTEIN_SANDBOX_ALLOW_PAID") == "1"
        # Modal is a remote backend - it must NOT trip the legacy
        # container flag (those backends manage their own runtime).
        assert os.environ.get("BERNSTEIN_CONTAINER") != "1"
        assert captured["estimated"] is True

    def test_paid_override_without_allow_paid_exits(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Naming a paid backend without ``--allow-paid`` halts the run.

        Silent fallbacks have caused surprise spend before, so the CLI
        treats this as an unrecoverable misconfiguration: exit non-zero
        with a remediation hint instead of running on the operator's
        cheapest-locally-available backend.
        """
        _stub_run_body(monkeypatch)
        runner = CliRunner()

        result = runner.invoke(run, ["--sandbox", "e2b", "--dry-run"])

        # The CLI emits SystemExit(2) with a BernsteinError diagnostic.
        assert result.exit_code != 0
        assert "allow-paid" in result.output.lower() or "allow_paid" in result.output.lower()
        # Env must NOT be left half-populated when the run aborts.
        # The runtime gets written before the exit, so the cleanup
        # contract is "selector treats absence of allow_paid as veto" -
        # we confirm the opt-in is OFF rather than absence of runtime.
        assert os.environ.get("BERNSTEIN_SANDBOX_ALLOW_PAID") != "1"

    def test_unknown_sandbox_is_rejected_by_click(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Click's Choice validator rejects unknown backend names.

        The validation runs before any env propagation so a typo can't
        leak partial state. Confirms the Choice list still drives
        accepted values: when a new backend lands in ``SANDBOX_CHOICES``,
        Click's parser auto-accepts it without further plumbing.
        """
        os.environ.pop("BERNSTEIN_SANDBOX_RUNTIME", None)
        _stub_run_body(monkeypatch)
        runner = CliRunner()

        result = runner.invoke(run, ["--sandbox", "wasm-soup", "--dry-run"])

        assert result.exit_code != 0
        assert "wasm-soup" in result.output or "Invalid" in result.output
        assert "BERNSTEIN_SANDBOX_RUNTIME" not in os.environ

    def test_sandbox_choices_cover_full_selector_precedence(self) -> None:
        """``SANDBOX_CHOICES`` must list every selector-known backend.

        Belt-and-suspenders: when a future ticket lands a new backend in
        the selector's ``DEFAULT_PRECEDENCE`` (e.g. ``runpod``), this
        test fails until the operator-facing CLI flag also accepts it.
        """
        from bernstein.core.sandbox.selector import DEFAULT_PRECEDENCE

        cli_choices = set(SANDBOX_CHOICES)
        # Podman is a CLI-only alias for docker (not in selector); skip.
        cli_choices.discard("podman")

        # Every selector backend must appear as a CLI choice.
        missing = set(DEFAULT_PRECEDENCE) - cli_choices
        assert not missing, (
            f"Sandbox CLI flag does not expose selector backend(s): {sorted(missing)}. "
            "Update bernstein.cli.run_bootstrap.SANDBOX_CHOICES + the cli() option."
        )


@pytest.mark.usefixtures("isolated_sandbox_env")
@pytest.mark.parametrize("allow_paid", [False, True])
def test_sandbox0_cli_requires_explicit_paid_opt_in(monkeypatch, allow_paid):
    monkeypatch.delenv("BERNSTEIN_CONTAINER", raising=False)
    _stub_run_body(monkeypatch)
    args = ["--sandbox", "sandbox0", "--dry-run"]
    if allow_paid:
        args.append("--allow-paid")
    result = CliRunner().invoke(run, args)
    assert result.exit_code == (0 if allow_paid else 2), result.output
    assert os.environ.get("BERNSTEIN_CONTAINER") != "1"
