"""Unit tests for the adapter contract checker.

Covers ``bernstein.adapters._contract`` end-to-end without spawning real
upstream CLIs: the subprocess helper is monkey-patched to feed
canned ``--help`` and ``models list`` outputs.

Refs: #1291.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from bernstein.adapters import _contract

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def claude_help() -> str:
    """Realistic abridged claude --help text containing all required flags."""
    return """Usage: claude [options] [prompt]

Options:
  --model <name>                Override the default model
  --effort <level>              Reasoning effort tier
  --permission-mode <mode>      Permission mode (ask|bypassPermissions)
  --max-turns <n>               Cap conversation turns
  --output-format <fmt>         text | json | stream-json
  --verbose                     Emit detailed trace events
  --include-hook-events         Forward hook events to stdout
  --no-session-persistence      Disable on-disk session log
"""


@pytest.fixture()
def codex_help() -> str:
    return """codex 0.1.0

USAGE:
  codex <SUBCOMMAND> [OPTIONS]

SUBCOMMANDS:
  exec      Run a one-shot prompt
  login     Authenticate

OPTIONS:
  --sandbox <profile>  Sandbox profile
  -m <model>           Model name
  --json               Emit JSON
  -o <path>            Output file
"""


@pytest.fixture()
def contract_dir(tmp_path: Path) -> Path:
    """A throwaway contracts/ directory."""
    d = tmp_path / "contracts"
    d.mkdir()
    return d


def _write_contract(contracts_dir: Path, payload: dict) -> None:
    name = payload["adapter"]
    (contracts_dir / f"{name}.yaml").write_text(yaml.safe_dump(payload), encoding="utf-8")


# ---------------------------------------------------------------------------
# ContractSpec.load - schema parsing
# ---------------------------------------------------------------------------


def test_load_parses_full_schema(contract_dir: Path) -> None:
    _write_contract(
        contract_dir,
        {
            "adapter": "demo",
            "binary": "demo-bin",
            "install": {"method": "npm", "spec": "demo@latest"},
            "auth": {
                "required_for_help": True,
                "required_for_models": True,
                "secret_env": "DEMO_KEY",
            },
            "required_flags": ["--alpha", "--beta"],
            "required_subcommands": ["run"],
            "expected_models": {
                "command": ["demo-bin", "models", "list"],
                "required_present": ["demo-1", "demo-2"],
            },
        },
    )
    spec = _contract.ContractSpec.load("demo", contracts_dir=contract_dir)
    assert spec.adapter == "demo"
    assert spec.binary == "demo-bin"
    assert spec.install_method == "npm"
    assert spec.install_spec == "demo@latest"
    assert spec.auth_required_for_help is True
    assert spec.auth_required_for_models is True
    assert spec.auth_secret_env == "DEMO_KEY"
    assert spec.required_flags == ("--alpha", "--beta")
    assert spec.required_subcommands == ("run",)
    assert spec.models_command == ("demo-bin", "models", "list")
    assert spec.models_required_present == ("demo-1", "demo-2")


def test_load_missing_raises(contract_dir: Path) -> None:
    with pytest.raises(FileNotFoundError):
        _contract.ContractSpec.load("nope", contracts_dir=contract_dir)


def test_load_defaults_safe(contract_dir: Path) -> None:
    _write_contract(contract_dir, {"adapter": "bare", "binary": "bare"})
    spec = _contract.ContractSpec.load("bare", contracts_dir=contract_dir)
    assert spec.auth_required_for_help is False
    assert spec.required_flags == ()
    assert spec.required_subcommands == ()
    assert spec.models_command == ()
    assert spec.help_command == ()
    assert spec.resolved_help_command() == ["bare", "--help"]


def test_help_command_override(contract_dir: Path) -> None:
    _write_contract(
        contract_dir,
        {
            "adapter": "sub",
            "binary": "sub",
            "help_command": ["sub", "run", "--help"],
        },
    )
    spec = _contract.ContractSpec.load("sub", contracts_dir=contract_dir)
    assert spec.resolved_help_command() == ["sub", "run", "--help"]


def test_check_contract_uses_help_command_override(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = _contract.ContractSpec(
        adapter="sub",
        binary="sub",
        install_method="",
        install_spec="",
        auth_required_for_help=False,
        auth_required_for_models=False,
        auth_secret_env="",
        required_flags=("--flag",),
        required_subcommands=(),
        help_command=("sub", "run", "--help"),
        models_command=(),
        models_required_present=(),
    )
    monkeypatch.setattr(_contract.shutil, "which", lambda _name: "/fake/bin/sub")
    monkeypatch.setattr(
        _contract,
        "_run_capture",
        _make_run_capture({("sub", "run", "--help"): (0, "Usage: sub run --flag <x>\n")}),
    )
    result = _contract.check_contract(spec)
    assert result.passed is True


# ---------------------------------------------------------------------------
# Capability evaluation
# ---------------------------------------------------------------------------


def test_capability_pass_when_all_present(claude_help: str) -> None:
    spec = _contract.ContractSpec(
        adapter="claude",
        binary="claude",
        install_method="",
        install_spec="",
        auth_required_for_help=False,
        auth_required_for_models=False,
        auth_secret_env="",
        required_flags=(
            "--model",
            "--output-format",
            "--max-turns",
            "--no-session-persistence",
        ),
        required_subcommands=(),
        help_command=(),
        models_command=(),
        models_required_present=(),
    )
    failures = _contract._capability_failures(spec, claude_help)
    assert failures == []


def test_capability_flag_missing_fails(claude_help: str) -> None:
    spec = _contract.ContractSpec(
        adapter="claude",
        binary="claude",
        install_method="",
        install_spec="",
        auth_required_for_help=False,
        auth_required_for_models=False,
        auth_secret_env="",
        required_flags=("--phantom-flag",),
        required_subcommands=(),
        help_command=(),
        models_command=(),
        models_required_present=(),
    )
    failures = _contract._capability_failures(spec, claude_help)
    assert len(failures) == 1
    assert "--phantom-flag" in failures[0]


def test_capability_subcommand_token_boundary(codex_help: str) -> None:
    spec = _contract.ContractSpec(
        adapter="codex",
        binary="codex",
        install_method="",
        install_spec="",
        auth_required_for_help=False,
        auth_required_for_models=False,
        auth_secret_env="",
        required_flags=(),
        required_subcommands=("exec", "login"),
        help_command=(),
        models_command=(),
        models_required_present=(),
    )
    failures = _contract._capability_failures(spec, codex_help)
    assert failures == []


def test_capability_subcommand_not_substring_match() -> None:
    """``run`` must not be satisfied by ``runs`` in help text."""
    spec = _contract.ContractSpec(
        adapter="x",
        binary="x",
        install_method="",
        install_spec="",
        auth_required_for_help=False,
        auth_required_for_models=False,
        auth_secret_env="",
        required_flags=(),
        required_subcommands=("run",),
        help_command=(),
        models_command=(),
        models_required_present=(),
    )
    help_text = "Subcommands:\n  runs   Manage runs\n"
    failures = _contract._capability_failures(spec, help_text)
    assert len(failures) == 1


def test_capability_match_is_case_insensitive() -> None:
    spec = _contract.ContractSpec(
        adapter="x",
        binary="x",
        install_method="",
        install_spec="",
        auth_required_for_help=False,
        auth_required_for_models=False,
        auth_secret_env="",
        required_flags=("--Yolo",),
        required_subcommands=(),
        help_command=(),
        models_command=(),
        models_required_present=(),
    )
    assert _contract._capability_failures(spec, "Usage: x --yolo <prompt>") == []


def test_ansi_stripped_before_match() -> None:
    spec = _contract.ContractSpec(
        adapter="x",
        binary="x",
        install_method="",
        install_spec="",
        auth_required_for_help=False,
        auth_required_for_models=False,
        auth_secret_env="",
        required_flags=("--yolo",),
        required_subcommands=(),
        help_command=(),
        models_command=(),
        models_required_present=(),
    )
    # Colourised help: the flag is wrapped in SGR escape codes.
    ansi_help = "Usage: x \x1b[1m--yolo\x1b[0m <prompt>\n"
    assert _contract._capability_failures(spec, ansi_help) == []


# ---------------------------------------------------------------------------
# check_contract - full pass/fail paths
# ---------------------------------------------------------------------------


def _make_run_capture(
    canned: dict[tuple[str, ...], tuple[int, str]],
) -> object:
    """Return a fake ``_run_capture`` that picks responses by argv."""

    def _fake(cmd: list[str], *, timeout: int, env: dict[str, str] | None = None):
        key = tuple(cmd)
        return canned[key]

    return _fake


def test_check_contract_passes(monkeypatch: pytest.MonkeyPatch, claude_help: str) -> None:
    spec = _contract.ContractSpec(
        adapter="claude",
        binary="claude",
        install_method="",
        install_spec="",
        auth_required_for_help=False,
        auth_required_for_models=False,
        auth_secret_env="",
        required_flags=("--model", "--output-format"),
        required_subcommands=(),
        help_command=(),
        models_command=(),
        models_required_present=(),
    )
    monkeypatch.setattr(_contract.shutil, "which", lambda _name: "/fake/bin/claude")
    monkeypatch.setattr(
        _contract,
        "_run_capture",
        _make_run_capture({("claude", "--help"): (0, claude_help)}),
    )
    result = _contract.check_contract(spec)
    assert result.passed is True
    assert result.binary_installed is True
    assert result.capability_failures == []


def test_check_contract_capability_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = _contract.ContractSpec(
        adapter="x",
        binary="x",
        install_method="",
        install_spec="",
        auth_required_for_help=False,
        auth_required_for_models=False,
        auth_secret_env="",
        required_flags=("--gone",),
        required_subcommands=(),
        help_command=(),
        models_command=(),
        models_required_present=(),
    )
    monkeypatch.setattr(_contract.shutil, "which", lambda _name: "/fake/bin/x")
    monkeypatch.setattr(
        _contract,
        "_run_capture",
        _make_run_capture({("x", "--help"): (0, "Usage: x <prompt>\n")}),
    )
    result = _contract.check_contract(spec)
    assert result.passed is False
    assert len(result.capability_failures) == 1
    assert "--gone" in result.capability_failures[0]


def test_check_contract_help_nonzero_empty_output_is_runtime_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-zero --help exit with empty output is reported as a runtime
    failure, not as N "missing flag" entries against an empty haystack.

    This prevents a broken upstream CLI (e.g. an unrelated runtime error
    during --help) from producing misleading drift issues that look like
    every required flag was removed at once.
    """
    spec = _contract.ContractSpec(
        adapter="brittle",
        binary="brittle",
        install_method="",
        install_spec="",
        auth_required_for_help=False,
        auth_required_for_models=False,
        auth_secret_env="",
        required_flags=("--alpha", "--beta", "--gamma"),
        required_subcommands=(),
        help_command=(),
        models_command=(),
        models_required_present=(),
    )
    monkeypatch.setattr(_contract.shutil, "which", lambda _name: "/fake/bin/brittle")
    monkeypatch.setattr(
        _contract,
        "_run_capture",
        _make_run_capture({("brittle", "--help"): (1, "")}),
    )
    result = _contract.check_contract(spec)
    assert result.passed is False
    # Runtime failure surfaces on a dedicated field, not as N "missing
    # flag" entries. The capability list stays empty so the workflow can
    # distinguish a checker-degraded run from real contract drift.
    assert result.capability_failures == []
    assert "runtime failure" in result.runtime_failure
    assert "exited 1" in result.runtime_failure


def test_check_contract_help_nonzero_all_flags_missing_is_runtime_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-zero exit with output that lacks ALL required tokens is a
    runtime failure, not drift.

    Pattern seen in CI: an upstream CLI prints an error preamble plus a
    truncated usage stub, exits non-zero, and the truncated stub does
    not advertise any of the contract's required flags. Reporting every
    flag as missing is misleading; the real signal is the broken
    --help, which an operator needs to investigate.
    """
    spec = _contract.ContractSpec(
        adapter="stub",
        binary="stub",
        install_method="",
        install_spec="",
        auth_required_for_help=False,
        auth_required_for_models=False,
        auth_secret_env="",
        required_flags=("--alpha", "--beta"),
        required_subcommands=(),
        help_command=(),
        models_command=(),
        models_required_present=(),
    )
    monkeypatch.setattr(_contract.shutil, "which", lambda _name: "/fake/bin/stub")
    monkeypatch.setattr(
        _contract,
        "_run_capture",
        _make_run_capture({("stub", "--help"): (1, "error: missing API key\nusage: stub [-h]\n")}),
    )
    result = _contract.check_contract(spec)
    assert result.passed is False
    assert result.capability_failures == []
    assert "runtime failure" in result.runtime_failure
    assert "no required tokens advertised" in result.runtime_failure


def test_check_contract_help_nonzero_with_output_still_checks_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-zero help exit *with* output still drives flag matching.

    Some CLIs return non-zero from --help by design (e.g. they print help
    on usage error). As long as we see real help text, we should still
    detect flag drift normally.
    """
    spec = _contract.ContractSpec(
        adapter="grumpy",
        binary="grumpy",
        install_method="",
        install_spec="",
        auth_required_for_help=False,
        auth_required_for_models=False,
        auth_secret_env="",
        required_flags=("--alpha",),
        required_subcommands=(),
        help_command=(),
        models_command=(),
        models_required_present=(),
    )
    monkeypatch.setattr(_contract.shutil, "which", lambda _name: "/fake/bin/grumpy")
    monkeypatch.setattr(
        _contract,
        "_run_capture",
        _make_run_capture({("grumpy", "--help"): (2, "Usage: grumpy --alpha <x>\n")}),
    )
    result = _contract.check_contract(spec)
    assert result.passed is True
    assert result.capability_failures == []


def test_check_contract_help_zero_exit_all_flags_missing_is_runtime_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A *zero* exit whose output lacks ALL required tokens is still a runtime
    failure, not drift.

    Regression for issue #2488: some CLIs ship a redesigned or paginated
    --help that advertises none of the required flags yet still exits 0.
    Gating the broken-probe guard on a non-zero exit misclassified that as
    six-flag drift and paged an operator with a misleading "every flag
    removed" finding. The guard now fires regardless of exit code once every
    required token is absent.
    """
    spec = _contract.ContractSpec(
        adapter="redesigned",
        binary="redesigned",
        install_method="",
        install_spec="",
        auth_required_for_help=False,
        auth_required_for_models=False,
        auth_secret_env="",
        required_flags=("--alpha", "--beta", "--gamma"),
        required_subcommands=(),
        help_command=(),
        models_command=(),
        models_required_present=(),
    )
    monkeypatch.setattr(_contract.shutil, "which", lambda _name: "/fake/bin/redesigned")
    monkeypatch.setattr(
        _contract,
        "_run_capture",
        _make_run_capture({("redesigned", "--help"): (0, "redesigned 3.13 - see docs for the new surface.\n")}),
    )
    result = _contract.check_contract(spec)
    assert result.passed is False
    assert result.capability_failures == []
    assert "runtime failure" in result.runtime_failure
    assert "no required tokens advertised" in result.runtime_failure


def test_check_contract_help_zero_exit_partial_miss_is_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A zero exit with *some* required tokens present is genuine drift.

    The broken-probe guard must only swallow a wholesale surface loss; a
    partial miss (at least one required token still advertised) is a real
    contract regression and must keep reporting per-flag drift.
    """
    spec = _contract.ContractSpec(
        adapter="partial",
        binary="partial",
        install_method="",
        install_spec="",
        auth_required_for_help=False,
        auth_required_for_models=False,
        auth_secret_env="",
        required_flags=("--alpha", "--beta", "--gamma"),
        required_subcommands=(),
        help_command=(),
        models_command=(),
        models_required_present=(),
    )
    monkeypatch.setattr(_contract.shutil, "which", lambda _name: "/fake/bin/partial")
    monkeypatch.setattr(
        _contract,
        "_run_capture",
        _make_run_capture({("partial", "--help"): (0, "usage: partial [--alpha A] [--beta B]\n")}),
    )
    result = _contract.check_contract(spec)
    assert result.passed is False
    assert result.runtime_failure == ""
    assert len(result.capability_failures) == 1
    assert "--gamma" in result.capability_failures[0]


def test_check_contract_binary_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = _contract.ContractSpec(
        adapter="ghost",
        binary="ghost",
        install_method="",
        install_spec="",
        auth_required_for_help=False,
        auth_required_for_models=False,
        auth_secret_env="",
        required_flags=("--anything",),
        required_subcommands=(),
        help_command=(),
        models_command=(),
        models_required_present=(),
    )
    monkeypatch.setattr(_contract.shutil, "which", lambda _name: None)
    result = _contract.check_contract(spec)
    assert result.binary_installed is False
    assert result.skipped_reason == "ghost not installed"
    assert result.passed is False


def test_check_contract_model_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = _contract.ContractSpec(
        adapter="demo",
        binary="demo",
        install_method="",
        install_spec="",
        auth_required_for_help=False,
        auth_required_for_models=True,
        auth_secret_env="DEMO_KEY",
        required_flags=(),
        required_subcommands=(),
        help_command=(),
        models_command=("demo", "models", "list"),
        models_required_present=("demo-1",),
    )
    monkeypatch.setenv("DEMO_KEY", "secret")
    monkeypatch.setattr(_contract.shutil, "which", lambda _name: "/fake/bin/demo")
    monkeypatch.setattr(
        _contract,
        "_run_capture",
        _make_run_capture(
            {
                ("demo", "--help"): (0, "Usage: demo\n"),
                ("demo", "models", "list"): (0, "demo-1\ndemo-2\ndemo-3\n"),
            }
        ),
    )
    result = _contract.check_contract(spec)
    assert result.passed is True
    assert result.models_checked is True
    assert result.model_failures == []


def test_check_contract_model_missing_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = _contract.ContractSpec(
        adapter="demo",
        binary="demo",
        install_method="",
        install_spec="",
        auth_required_for_help=False,
        auth_required_for_models=True,
        auth_secret_env="DEMO_KEY",
        required_flags=(),
        required_subcommands=(),
        help_command=(),
        models_command=("demo", "models", "list"),
        models_required_present=("demo-1", "demo-9"),
    )
    monkeypatch.setenv("DEMO_KEY", "secret")
    monkeypatch.setattr(_contract.shutil, "which", lambda _name: "/fake/bin/demo")
    monkeypatch.setattr(
        _contract,
        "_run_capture",
        _make_run_capture(
            {
                ("demo", "--help"): (0, "Usage: demo\n"),
                ("demo", "models", "list"): (0, "demo-1\ndemo-2\n"),
            }
        ),
    )
    result = _contract.check_contract(spec)
    assert result.passed is False
    assert len(result.model_failures) == 1
    assert "demo-9" in result.model_failures[0]


def test_check_contract_model_skipped_without_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = _contract.ContractSpec(
        adapter="demo",
        binary="demo",
        install_method="",
        install_spec="",
        auth_required_for_help=False,
        auth_required_for_models=True,
        auth_secret_env="DEMO_KEY",
        required_flags=(),
        required_subcommands=(),
        help_command=(),
        models_command=("demo", "models", "list"),
        models_required_present=("demo-1",),
    )
    monkeypatch.delenv("DEMO_KEY", raising=False)
    monkeypatch.setattr(_contract.shutil, "which", lambda _name: "/fake/bin/demo")
    monkeypatch.setattr(
        _contract,
        "_run_capture",
        _make_run_capture({("demo", "--help"): (0, "Usage: demo\n")}),
    )
    result = _contract.check_contract(spec)
    # Help-only run: passes capability, model check skipped, models_checked=False.
    assert result.capability_failures == []
    assert result.model_failures == []
    assert result.models_checked is False
    assert "DEMO_KEY" in result.skipped_reason
    # ``passed`` is True because there are no failures.
    assert result.passed is True


def test_check_contract_help_requires_auth_and_secret_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _contract.ContractSpec(
        adapter="closed",
        binary="closed",
        install_method="",
        install_spec="",
        auth_required_for_help=True,
        auth_required_for_models=False,
        auth_secret_env="CLOSED_KEY",
        required_flags=("--anything",),
        required_subcommands=(),
        help_command=(),
        models_command=(),
        models_required_present=(),
    )
    monkeypatch.delenv("CLOSED_KEY", raising=False)
    monkeypatch.setattr(_contract.shutil, "which", lambda _name: "/fake/bin/closed")
    result = _contract.check_contract(spec)
    assert result.binary_installed is True
    assert "CLOSED_KEY" in result.skipped_reason
    # Capability check never ran, so failures list is empty; passed is False
    # because the binary needs auth and we don't have it.
    assert result.capability_failures == []


# ---------------------------------------------------------------------------
# list_contracts - repo-shipped contracts
# ---------------------------------------------------------------------------


def test_repo_contracts_are_loadable() -> None:
    """Every shipped contract must parse without error."""
    names = _contract.list_contracts()
    assert len(names) >= 15, f"expected at least 15 adapter contracts, got {len(names)}"
    for n in names:
        spec = _contract.ContractSpec.load(n)
        assert spec.binary, f"contract {n} is missing a binary"
        # Either the contract specifies real capability assertions or it
        # is documented as help-only.
        # (Both branches are valid; this just ensures the field exists.)
        assert isinstance(spec.required_flags, tuple)
