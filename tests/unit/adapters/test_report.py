"""Unit tests for ``bernstein.adapters.report``.

The conformance + capability report is the data spine for
``bernstein adapters check``. These tests pin every branch:

* Binary present and version captured.
* Binary missing -> ``skip`` with ``binary missing``.
* Binary present but ``--help`` lacks a required flag -> ``fail``.
* JSON output round-trips through ``json.loads``.
* ``build_report(only=...)`` filters and raises on miss.
* The Click command's exit code obeys ``--strict``.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from bernstein.adapters import report as report_mod
from bernstein.adapters.base import CLIAdapter
from bernstein.adapters.report import (
    CONFORMANCE_FAIL,
    CONFORMANCE_OK,
    CONFORMANCE_SKIP,
    AdapterReport,
    AdapterStatus,
    ConformanceVerdictPayload,
    ReportSummary,
    _binary_for_adapter,
    _capture_version,
    _contract_capabilities,
    _contract_hash,
    _module_mtime_utc,
    _resolve_module_path,
    _status_for_one,
    _summarize,
    build_report,
    check_adapter_in_process,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _Stub(CLIAdapter):
    """Bare minimum CLIAdapter for tests; never spawns."""

    def name(self) -> str:
        return "stub"

    def spawn(
        self,
        *,
        prompt: str,
        workdir: Path,
        model_config: Any,
        session_id: str,
        mcp_config: dict[str, Any] | None = None,
        timeout_seconds: int = 1800,
        task_scope: str = "medium",
        budget_multiplier: float = 1.0,
        system_addendum: str = "",
    ) -> Any:
        raise NotImplementedError


@pytest.fixture
def contracts_tmp(tmp_path: Path) -> Path:
    """Empty contracts directory test fixture."""
    d = tmp_path / "contracts"
    d.mkdir()
    return d


def _write_contract(
    directory: Path,
    name: str,
    *,
    binary: str = "stub",
    flags: tuple[str, ...] = ("--model",),
    subcommands: tuple[str, ...] = (),
) -> Path:
    """Write a minimal contract YAML for tests."""
    flags_yaml = ("\n" + "\n".join(f"  - {f!r}" for f in flags)) if flags else " []"
    sub_yaml = ("\n" + "\n".join(f"  - {s!r}" for s in subcommands)) if subcommands else " []"
    body = (
        f"adapter: {name}\n"
        f"binary: {binary}\n"
        "install:\n  method: npm\n  spec: ''\n"
        "auth:\n  required_for_help: false\n  required_for_models: false\n  secret_env: ''\n"
        f"required_flags:{flags_yaml}\n"
        f"required_subcommands:{sub_yaml}\n"
        "expected_models:\n  command: []\n  required_present: []\n"
    )
    path = directory / f"{name}.yaml"
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Dataclass + helper tests
# ---------------------------------------------------------------------------


def test_adapter_status_to_dict_emits_sorted_capabilities() -> None:
    """``capabilities`` round-trips as a sorted list."""
    s = AdapterStatus(
        name="x",
        module_path="x.py",
        binary_resolved=None,
        version_string=None,
        capabilities=frozenset({"z", "a", "m"}),
        conformance=CONFORMANCE_SKIP,
        conformance_detail="",
        last_modified_utc="",
        contract_hash="",
    )
    payload = s.to_dict()
    assert payload["capabilities"] == ["a", "m", "z"]


def test_adapter_status_to_dict_round_trips_through_json() -> None:
    """A status dict serialises to JSON and back without loss."""
    s = AdapterStatus(
        name="x",
        module_path="x.py",
        binary_resolved="/usr/local/bin/x",
        version_string="1.0",
        capabilities=frozenset({"--model"}),
        conformance=CONFORMANCE_OK,
        conformance_detail="",
        last_modified_utc="2026-05-17T00:00:00+00:00",
        contract_hash="deadbeef",
    )
    text = json.dumps(s.to_dict())
    loaded = json.loads(text)
    assert loaded["name"] == "x"
    assert loaded["capabilities"] == ["--model"]
    assert loaded["conformance"] == "ok"


def test_report_summary_to_dict_returns_int_counts() -> None:
    """Summary counts serialise as ints (no leaked sets)."""
    s = ReportSummary(total=3, reachable=2, conform=1, fail=1, skip=1)
    assert s.to_dict() == {"total": 3, "reachable": 2, "conform": 1, "fail": 1, "skip": 1}


def test_adapter_report_to_dict_keys() -> None:
    """The top-level payload always has ``adapters`` and ``summary`` keys."""
    r = AdapterReport()
    payload = r.to_dict()
    assert set(payload.keys()) == {"adapters", "summary"}


def test_adapter_report_to_json_is_parseable() -> None:
    """``to_json`` emits valid JSON that round-trips through ``json.loads``."""
    s = AdapterStatus(
        name="x",
        module_path="x.py",
        binary_resolved="/usr/bin/x",
        version_string="1.0",
        capabilities=frozenset({"--model"}),
        conformance=CONFORMANCE_OK,
        conformance_detail="",
        last_modified_utc="",
        contract_hash="",
    )
    r = AdapterReport(adapters=(s,), summary=ReportSummary(1, 1, 1, 0, 0))
    parsed = json.loads(r.to_json())
    assert parsed["summary"]["conform"] == 1
    assert parsed["adapters"][0]["name"] == "x"


def test_summarize_counts_each_verdict_class() -> None:
    """Aggregator splits rows into reachable/conform/fail/skip buckets."""
    rows: tuple[AdapterStatus, ...] = (
        AdapterStatus("a", "a.py", "/usr/bin/a", "1", frozenset(), CONFORMANCE_OK, "", "", ""),
        AdapterStatus("b", "b.py", None, None, frozenset(), CONFORMANCE_SKIP, "binary missing", "", ""),
        AdapterStatus("c", "c.py", "/usr/bin/c", "1", frozenset(), CONFORMANCE_FAIL, "missing flag", "", ""),
    )
    s = _summarize(rows)
    assert s.total == 3
    assert s.reachable == 2
    assert s.conform == 1
    assert s.fail == 1
    assert s.skip == 1


def test_summarize_empty_input_returns_zero_summary() -> None:
    """No rows -> all counters zero."""
    s = _summarize(())
    assert s == ReportSummary(0, 0, 0, 0, 0)


def test_binary_for_adapter_uses_override_table() -> None:
    """Adapter names with binary overrides take the override."""
    assert _binary_for_adapter("q_dev") == "q"
    assert _binary_for_adapter("devin_terminal") == "devin"
    assert _binary_for_adapter("composio") == "ao"


def test_binary_for_adapter_falls_back_to_name() -> None:
    """Unknown registry keys default to their own name as the binary."""
    assert _binary_for_adapter("custom-third-party") == "custom-third-party"


def test_binary_for_adapter_empty_string_for_no_binary_adapters() -> None:
    """Adapters with no upstream binary surface as empty string."""
    assert _binary_for_adapter("mock") == ""
    assert _binary_for_adapter("generic") == ""


def test_contract_hash_returns_empty_when_contract_missing(contracts_tmp: Path) -> None:
    """Missing contract files surface as an empty hash, not a crash."""
    assert _contract_hash("nonexistent", contracts_dir=contracts_tmp) == ""


def test_contract_hash_is_stable_for_same_bytes(contracts_tmp: Path) -> None:
    """Two reads of the same contract produce the same digest."""
    _write_contract(contracts_tmp, "stub", flags=("--model",))
    assert _contract_hash("stub", contracts_dir=contracts_tmp) == _contract_hash("stub", contracts_dir=contracts_tmp)


def test_contract_hash_changes_when_bytes_change(contracts_tmp: Path) -> None:
    """Editing the contract YAML invalidates the digest."""
    _write_contract(contracts_tmp, "stub", flags=("--model",))
    first = _contract_hash("stub", contracts_dir=contracts_tmp)
    _write_contract(contracts_tmp, "stub", flags=("--model", "--effort"))
    second = _contract_hash("stub", contracts_dir=contracts_tmp)
    assert first != second


def test_contract_capabilities_collects_flags_and_subs() -> None:
    """Both required flags and subcommands feed the capability set."""
    from bernstein.adapters._contract import ContractSpec

    spec = ContractSpec(
        adapter="x",
        binary="x",
        install_method="",
        install_spec="",
        auth_required_for_help=False,
        auth_required_for_models=False,
        auth_secret_env="",
        required_flags=("--a", "--b"),
        required_subcommands=("run",),
        help_command=(),
        models_command=(),
        models_required_present=(),
    )
    assert _contract_capabilities(spec) == frozenset({"--a", "--b", "run"})


def test_resolve_module_path_for_unknown_returns_marker() -> None:
    """An object inspect can't reach surfaces the fallback marker."""

    class Inline:
        pass

    # Inline classes living in a test module typically resolve to the
    # test file itself - either way the helper must produce a string.
    result = _resolve_module_path(Inline)
    assert isinstance(result, str)
    assert result  # non-empty


def test_module_mtime_utc_returns_iso_or_empty() -> None:
    """mtime helper returns an ISO timestamp for inspectable adapters."""
    ts = _module_mtime_utc(_Stub)
    assert ts == "" or ("T" in ts and (ts.endswith("+00:00") or ts.endswith("UTC")))


# ---------------------------------------------------------------------------
# capture_version
# ---------------------------------------------------------------------------


def test_capture_version_returns_first_line_of_output() -> None:
    """The captured version is the first stripped output line."""
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="1.0.42\nmore\n", stderr="")
    with patch.object(report_mod.subprocess, "run", return_value=completed):
        assert _capture_version("anything") == "1.0.42"


def test_capture_version_falls_back_to_stderr_when_stdout_empty() -> None:
    """Some CLIs emit ``--version`` on stderr; we still capture it."""
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="banner 2.1\n")
    with patch.object(report_mod.subprocess, "run", return_value=completed):
        assert _capture_version("x") == "banner 2.1"


def test_capture_version_returns_none_on_filenotfound() -> None:
    """A missing binary never raises - we just return ``None``."""
    with patch.object(report_mod.subprocess, "run", side_effect=FileNotFoundError()):
        assert _capture_version("missing") is None


def test_capture_version_returns_none_on_timeout() -> None:
    """A timeout swallows cleanly without bubbling up."""
    with patch.object(
        report_mod.subprocess,
        "run",
        side_effect=subprocess.TimeoutExpired(cmd="x", timeout=5),
    ):
        assert _capture_version("x") is None


def test_capture_version_returns_none_on_empty_output() -> None:
    """Empty stdout+stderr never produces a fake version string."""
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with patch.object(report_mod.subprocess, "run", return_value=completed):
        assert _capture_version("x") is None


def test_capture_version_empty_binary_returns_none() -> None:
    """No binary name means no subprocess - return ``None`` immediately."""
    assert _capture_version("") is None


def test_capture_version_strips_ansi_escapes() -> None:
    """ANSI escape sequences in version output do not leak."""
    completed = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout="\x1b[1mclaude-code\x1b[0m 1.0.42\n",
        stderr="",
    )
    with patch.object(report_mod.subprocess, "run", return_value=completed):
        out = _capture_version("claude")
        assert out == "claude-code 1.0.42"


# ---------------------------------------------------------------------------
# check_adapter_in_process
# ---------------------------------------------------------------------------


def test_check_adapter_no_contract_yields_skip(contracts_tmp: Path) -> None:
    """No contract -> ``skip`` with the ``no contract`` reason."""
    v = check_adapter_in_process("ghost", binary_resolved=None, contracts_dir=contracts_tmp)
    assert v.verdict == CONFORMANCE_SKIP
    assert v.detail == "no contract"
    assert v.capabilities == frozenset()


def test_check_adapter_binary_missing_yields_skip(contracts_tmp: Path) -> None:
    """Contract on disk but no binary -> ``skip``/``binary missing``."""
    _write_contract(contracts_tmp, "stub", flags=("--model",))
    v = check_adapter_in_process("stub", binary_resolved=None, contracts_dir=contracts_tmp)
    assert v.verdict == CONFORMANCE_SKIP
    assert v.detail == "binary missing"
    assert v.capabilities == frozenset({"--model"})


def test_check_adapter_help_failure_yields_skip(contracts_tmp: Path) -> None:
    """``--help`` blowing up surfaces as ``skip`` not ``fail``."""
    _write_contract(contracts_tmp, "stub", flags=("--model",))
    with patch.object(report_mod.subprocess, "run", side_effect=FileNotFoundError()):
        v = check_adapter_in_process("stub", binary_resolved="/usr/bin/stub", contracts_dir=contracts_tmp)
    assert v.verdict == CONFORMANCE_SKIP
    assert "--help failed" in v.detail


def test_check_adapter_help_timeout_yields_skip(contracts_tmp: Path) -> None:
    """A slow CLI is recorded as ``skip``/``--help timed out``."""
    _write_contract(contracts_tmp, "stub", flags=("--model",))
    with patch.object(
        report_mod.subprocess,
        "run",
        side_effect=subprocess.TimeoutExpired(cmd="stub", timeout=5),
    ):
        v = check_adapter_in_process("stub", binary_resolved="/usr/bin/stub", contracts_dir=contracts_tmp)
    assert v.verdict == CONFORMANCE_SKIP
    assert "timed out" in v.detail


def test_check_adapter_help_passing_yields_ok(contracts_tmp: Path) -> None:
    """Help text containing every required flag earns the ``ok`` verdict."""
    _write_contract(contracts_tmp, "stub", flags=("--model", "--effort"))
    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="usage: stub [--model M] [--effort E]\n", stderr=""
    )
    with patch.object(report_mod.subprocess, "run", return_value=completed):
        v = check_adapter_in_process("stub", binary_resolved="/usr/bin/stub", contracts_dir=contracts_tmp)
    assert v.verdict == CONFORMANCE_OK
    assert v.detail == ""
    assert v.capabilities == frozenset({"--model", "--effort"})


def test_check_adapter_help_missing_flag_yields_fail(contracts_tmp: Path) -> None:
    """Help text missing a required flag -> ``fail`` with a useful note."""
    _write_contract(contracts_tmp, "stub", flags=("--model", "--effort"))
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="usage: stub [--model M]\n", stderr="")
    with patch.object(report_mod.subprocess, "run", return_value=completed):
        v = check_adapter_in_process("stub", binary_resolved="/usr/bin/stub", contracts_dir=contracts_tmp)
    assert v.verdict == CONFORMANCE_FAIL
    assert "--effort" in v.detail


def test_check_adapter_missing_subcommand_yields_fail(contracts_tmp: Path) -> None:
    """A missing required subcommand also produces ``fail``."""
    _write_contract(
        contracts_tmp,
        "stub",
        flags=(),
        subcommands=("plan",),
    )
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="usage: stub --help\n", stderr="")
    with patch.object(report_mod.subprocess, "run", return_value=completed):
        v = check_adapter_in_process("stub", binary_resolved="/usr/bin/stub", contracts_dir=contracts_tmp)
    assert v.verdict == CONFORMANCE_FAIL
    assert "plan" in v.detail


def test_check_adapter_subcommand_present_yields_ok(contracts_tmp: Path) -> None:
    """Subcommand present at a token boundary passes."""
    _write_contract(contracts_tmp, "stub", flags=(), subcommands=("run",))
    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="Commands:\n  run    Run the agent\n  list   ...\n", stderr=""
    )
    with patch.object(report_mod.subprocess, "run", return_value=completed):
        v = check_adapter_in_process("stub", binary_resolved="/usr/bin/stub", contracts_dir=contracts_tmp)
    assert v.verdict == CONFORMANCE_OK


def test_check_adapter_all_flags_missing_yields_skip_not_fail(contracts_tmp: Path) -> None:
    """Help advertising none of the required tokens is a probe failure (skip).

    Regression for issue #2488: an installed binary whose --help advertised
    none of the six aider flags was misreported as six-flag contract drift
    (one "missing flag" line per flag), paging an operator even though the
    flags were all still present. An adapter cannot legitimately drop its
    entire required surface at once, so this is a broken/redesigned probe and
    must record ``skip`` (investigate), never ``fail`` (drift).
    """
    _write_contract(contracts_tmp, "stub", flags=("--model", "--message", "--yes-always"))
    # A redesigned help banner that advertises none of the required flags.
    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="Stub 3.13 - see `stub docs` for usage.\n", stderr=""
    )
    with patch.object(report_mod.subprocess, "run", return_value=completed):
        v = check_adapter_in_process("stub", binary_resolved="/usr/bin/stub", contracts_dir=contracts_tmp)
    assert v.verdict == CONFORMANCE_SKIP
    assert "none of" in v.detail
    assert "3 required tokens" in v.detail


def test_check_adapter_empty_help_yields_skip(contracts_tmp: Path) -> None:
    """An installed binary whose --help prints nothing is a probe failure."""
    _write_contract(contracts_tmp, "stub", flags=("--model", "--message"))
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with patch.object(report_mod.subprocess, "run", return_value=completed):
        v = check_adapter_in_process("stub", binary_resolved="/usr/bin/stub", contracts_dir=contracts_tmp)
    assert v.verdict == CONFORMANCE_SKIP
    assert "no output" in v.detail


def test_check_adapter_partial_miss_still_fails(contracts_tmp: Path) -> None:
    """A partial miss (some tokens present) is genuine drift and stays ``fail``."""
    _write_contract(contracts_tmp, "stub", flags=("--model", "--message", "--yes-always"))
    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="usage: stub [--model M] [--message X]\n", stderr=""
    )
    with patch.object(report_mod.subprocess, "run", return_value=completed):
        v = check_adapter_in_process("stub", binary_resolved="/usr/bin/stub", contracts_dir=contracts_tmp)
    assert v.verdict == CONFORMANCE_FAIL
    assert "--yes-always" in v.detail


def test_check_adapter_single_flag_missing_stays_fail(contracts_tmp: Path) -> None:
    """A one-token contract losing its token is ordinary drift, not a probe
    failure: it cannot be told apart from a wholesale surface loss, so the
    broken-probe classification is deliberately not applied at one token."""
    _write_contract(contracts_tmp, "stub", flags=("--model",))
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="usage: stub [--other X]\n", stderr="")
    with patch.object(report_mod.subprocess, "run", return_value=completed):
        v = check_adapter_in_process("stub", binary_resolved="/usr/bin/stub", contracts_dir=contracts_tmp)
    assert v.verdict == CONFORMANCE_FAIL
    assert "--model" in v.detail


# ---------------------------------------------------------------------------
# build_report
# ---------------------------------------------------------------------------


def _stub_iter() -> Any:
    """Return a one-off two-adapter registry iterator for build_report tests."""

    def _gen() -> Any:
        yield "alpha", _Stub
        yield "beta", _Stub

    return _gen


def test_build_report_sorts_by_adapter_name(contracts_tmp: Path) -> None:
    """The report is always sorted alphabetically by adapter name."""

    def _reversed() -> Any:
        yield "zzz", _Stub
        yield "aaa", _Stub

    with patch("bernstein.adapters.registry.iter_adapter_specs", _reversed):
        report = build_report(contracts_dir=contracts_tmp, capture_version=False)
    assert [a.name for a in report.adapters] == ["aaa", "zzz"]


def test_build_report_summary_matches_rows(contracts_tmp: Path) -> None:
    """``summary.total`` always equals ``len(adapters)``."""
    with patch("bernstein.adapters.registry.iter_adapter_specs", _stub_iter()):
        report = build_report(contracts_dir=contracts_tmp, capture_version=False)
    assert report.summary.total == len(report.adapters)


def test_build_report_only_filters_to_single_adapter(contracts_tmp: Path) -> None:
    """``only="alpha"`` yields exactly one row."""
    with patch("bernstein.adapters.registry.iter_adapter_specs", _stub_iter()):
        report = build_report(contracts_dir=contracts_tmp, capture_version=False, only="alpha")
    assert len(report.adapters) == 1
    assert report.adapters[0].name == "alpha"


def test_build_report_only_unknown_raises_keyerror(contracts_tmp: Path) -> None:
    """An unknown ``only`` raises ``KeyError`` for the CLI to translate."""
    with patch("bernstein.adapters.registry.iter_adapter_specs", _stub_iter()):
        with pytest.raises(KeyError):
            build_report(contracts_dir=contracts_tmp, capture_version=False, only="missing")


def test_build_report_skips_capture_version_when_disabled(contracts_tmp: Path) -> None:
    """Test mode never invokes a real subprocess."""
    with patch("bernstein.adapters.registry.iter_adapter_specs", _stub_iter()):
        with patch.object(report_mod, "_capture_version") as cap:
            build_report(contracts_dir=contracts_tmp, capture_version=False)
        cap.assert_not_called()


def test_build_report_returns_immutable_adapters_tuple(contracts_tmp: Path) -> None:
    """``adapters`` is a tuple so callers cannot mutate the snapshot."""
    with patch("bernstein.adapters.registry.iter_adapter_specs", _stub_iter()):
        report = build_report(contracts_dir=contracts_tmp, capture_version=False)
    assert isinstance(report.adapters, tuple)


def test_build_report_uses_iter_adapter_specs_only_once(contracts_tmp: Path) -> None:
    """Each call materialises exactly one registry pass."""
    calls = {"n": 0}

    def _spy() -> Any:
        calls["n"] += 1
        yield "alpha", _Stub

    with patch("bernstein.adapters.registry.iter_adapter_specs", _spy):
        build_report(contracts_dir=contracts_tmp, capture_version=False)
    assert calls["n"] == 1


def test_build_report_captures_status_module_path(contracts_tmp: Path) -> None:
    """Row's ``module_path`` is populated even with the stub adapter."""
    with patch("bernstein.adapters.registry.iter_adapter_specs", _stub_iter()):
        report = build_report(contracts_dir=contracts_tmp, capture_version=False)
    assert all(isinstance(a.module_path, str) and a.module_path for a in report.adapters)


def test_build_report_real_registry_total_is_at_least_44() -> None:
    """The real registry has 44+ adapters."""
    report = build_report(capture_version=False)
    assert report.summary.total >= 44


def test_build_report_only_real_adapter_returns_one_row() -> None:
    """``only="claude"`` against the real registry yields a single row."""
    report = build_report(capture_version=False, only="claude")
    assert len(report.adapters) == 1
    assert report.adapters[0].name == "claude"


def test_status_for_one_returns_skip_when_no_binary(contracts_tmp: Path) -> None:
    """Mock adapter has no binary -> skip without capturing version."""
    s = _status_for_one("mock", _Stub, contracts_dir=contracts_tmp, capture_version=True)
    assert s.binary_resolved is None
    assert s.version_string is None
    assert s.conformance == CONFORMANCE_SKIP


def test_status_for_one_handles_unknown_adapter(contracts_tmp: Path) -> None:
    """Unknown adapter without a contract is a clean ``skip``."""
    s = _status_for_one("never-heard-of-it", _Stub, contracts_dir=contracts_tmp, capture_version=False)
    assert s.conformance == CONFORMANCE_SKIP


# ---------------------------------------------------------------------------
# JSON contract
# ---------------------------------------------------------------------------


def test_full_report_to_json_round_trips(contracts_tmp: Path) -> None:
    """Full report JSON is parseable and keys are stable."""
    with patch("bernstein.adapters.registry.iter_adapter_specs", _stub_iter()):
        report = build_report(contracts_dir=contracts_tmp, capture_version=False)
    parsed = json.loads(report.to_json())
    assert "adapters" in parsed and "summary" in parsed
    for row in parsed["adapters"]:
        assert {
            "name",
            "module_path",
            "binary_resolved",
            "version_string",
            "capabilities",
            "conformance",
            "conformance_detail",
            "last_modified_utc",
            "contract_hash",
        } <= set(row.keys())


def test_json_capabilities_are_sorted_list(contracts_tmp: Path) -> None:
    """JSON ``capabilities`` is a sorted list (deterministic for diffs)."""
    _write_contract(contracts_tmp, "alpha", flags=("--zeta", "--alpha"))
    with patch("bernstein.adapters.registry.iter_adapter_specs", _stub_iter()):
        report = build_report(contracts_dir=contracts_tmp, capture_version=False)
    payload = json.loads(report.to_json())
    for row in payload["adapters"]:
        assert row["capabilities"] == sorted(row["capabilities"])


def test_json_summary_total_matches_adapters_length(contracts_tmp: Path) -> None:
    """``summary.total`` always matches ``len(adapters)`` in JSON."""
    with patch("bernstein.adapters.registry.iter_adapter_specs", _stub_iter()):
        report = build_report(contracts_dir=contracts_tmp, capture_version=False)
    parsed = json.loads(report.to_json())
    assert parsed["summary"]["total"] == len(parsed["adapters"])


# ---------------------------------------------------------------------------
# Click command exit code semantics
# ---------------------------------------------------------------------------


def test_cli_check_strict_zero_when_no_failures(contracts_tmp: Path) -> None:
    """``--strict`` returns 0 when no row is ``fail``."""
    from click.testing import CliRunner

    from bernstein.cli.commands.adapters_cmd import adapters_check_cmd

    with patch("bernstein.adapters.registry.iter_adapter_specs", _stub_iter()):
        with patch("bernstein.adapters.report.CONTRACTS_DIR", contracts_tmp):
            result = CliRunner().invoke(adapters_check_cmd, ["--strict", "--format", "json"])
    assert result.exit_code == 0, result.output


def test_cli_check_strict_nonzero_when_failure_present(contracts_tmp: Path) -> None:
    """``--strict`` returns 1 when at least one row is ``fail``."""
    from click.testing import CliRunner

    from bernstein.cli.commands.adapters_cmd import adapters_check_cmd

    _write_contract(contracts_tmp, "alpha", flags=("--needed",))
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="usage: alpha\n", stderr="")

    def _iter() -> Any:
        yield "alpha", _Stub

    with patch.object(report_mod, "_binary_for_adapter", return_value="alpha"):
        with patch.object(report_mod.shutil, "which", return_value="/usr/bin/alpha"):
            with patch.object(report_mod.subprocess, "run", return_value=completed):
                with patch("bernstein.adapters.registry.iter_adapter_specs", _iter):
                    with patch("bernstein.adapters.report.CONTRACTS_DIR", contracts_tmp):
                        result = CliRunner().invoke(adapters_check_cmd, ["--strict", "--format", "json"])
    assert result.exit_code == 1, result.output


def test_cli_check_unknown_adapter_emits_error_and_exits_two(contracts_tmp: Path) -> None:
    """Unknown adapter NAME produces a useful stderr line and exit 2."""
    from click.testing import CliRunner

    from bernstein.cli.commands.adapters_cmd import adapters_check_cmd

    with patch("bernstein.adapters.registry.iter_adapter_specs", _stub_iter()):
        result = CliRunner().invoke(adapters_check_cmd, ["never-heard-of-it", "--format", "json"])
    assert result.exit_code == 2


def test_cli_check_default_format_is_table(contracts_tmp: Path) -> None:
    """Default ``--format`` is ``table`` and produces non-JSON output."""
    from click.testing import CliRunner

    from bernstein.cli.commands.adapters_cmd import adapters_check_cmd

    with patch("bernstein.adapters.registry.iter_adapter_specs", _stub_iter()):
        with patch("bernstein.adapters.report.CONTRACTS_DIR", contracts_tmp):
            result = CliRunner().invoke(adapters_check_cmd, [])
    assert result.exit_code == 0
    assert "alpha" in result.output
    # Must not start with "{" (would indicate JSON crept into the default).
    assert not result.output.lstrip().startswith("{")


def test_cli_check_json_format_is_parseable(contracts_tmp: Path) -> None:
    """``--format json`` emits a parseable document."""
    from click.testing import CliRunner

    from bernstein.cli.commands.adapters_cmd import adapters_check_cmd

    with patch("bernstein.adapters.registry.iter_adapter_specs", _stub_iter()):
        with patch("bernstein.adapters.report.CONTRACTS_DIR", contracts_tmp):
            result = CliRunner().invoke(adapters_check_cmd, ["--format", "json"])
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed["summary"]["total"] == 2


def test_cli_check_single_adapter_yields_one_row(contracts_tmp: Path) -> None:
    """``check <name>`` filters the report to a single adapter."""
    from click.testing import CliRunner

    from bernstein.cli.commands.adapters_cmd import adapters_check_cmd

    with patch("bernstein.adapters.registry.iter_adapter_specs", _stub_iter()):
        with patch("bernstein.adapters.report.CONTRACTS_DIR", contracts_tmp):
            result = CliRunner().invoke(adapters_check_cmd, ["alpha", "--format", "json"])
    parsed = json.loads(result.output)
    assert parsed["summary"]["total"] == 1
    assert parsed["adapters"][0]["name"] == "alpha"


def test_cli_check_table_includes_summary_footer(contracts_tmp: Path) -> None:
    """The Rich table renders a single-line footer summary."""
    from click.testing import CliRunner

    from bernstein.cli.commands.adapters_cmd import adapters_check_cmd

    with patch("bernstein.adapters.registry.iter_adapter_specs", _stub_iter()):
        with patch("bernstein.adapters.report.CONTRACTS_DIR", contracts_tmp):
            result = CliRunner().invoke(adapters_check_cmd, [])
    assert "adapters total" in result.output


def test_cli_check_strict_only_triggers_on_fail_not_skip(contracts_tmp: Path) -> None:
    """``--strict`` ignores ``skip`` rows (binary missing is expected)."""
    from click.testing import CliRunner

    from bernstein.cli.commands.adapters_cmd import adapters_check_cmd

    _write_contract(contracts_tmp, "alpha", flags=("--model",))

    def _iter() -> Any:
        yield "alpha", _Stub

    with patch("bernstein.adapters.registry.iter_adapter_specs", _iter):
        with patch("bernstein.adapters.report.CONTRACTS_DIR", contracts_tmp):
            with patch.object(report_mod.shutil, "which", return_value=None):
                result = CliRunner().invoke(adapters_check_cmd, ["--strict", "--format", "json"])
    assert result.exit_code == 0


def test_cli_list_status_runs_against_real_registry() -> None:
    """``list-status`` exits 0 against the real registry."""
    from click.testing import CliRunner

    from bernstein.cli.commands.adapters_cmd import adapters_list_status_cmd

    result = CliRunner().invoke(adapters_list_status_cmd, ["--format", "json"])
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed["summary"]["total"] >= 44


# ---------------------------------------------------------------------------
# Snapshot-style structural test for the table renderer
# ---------------------------------------------------------------------------


def test_render_table_rows_contains_every_adapter_name(contracts_tmp: Path) -> None:
    """The rendered Rich table mentions every adapter name in the report."""
    from bernstein.cli.commands.adapters_cmd import _render_table_rows

    with patch("bernstein.adapters.registry.iter_adapter_specs", _stub_iter()):
        report = build_report(contracts_dir=contracts_tmp, capture_version=False)
    rendered = _render_table_rows(report)
    for status in report.adapters:
        assert status.name in rendered


def test_render_table_rows_has_summary_footer(contracts_tmp: Path) -> None:
    """The renderer always appends the summary footer line."""
    from bernstein.cli.commands.adapters_cmd import _render_table_rows

    with patch("bernstein.adapters.registry.iter_adapter_specs", _stub_iter()):
        report = build_report(contracts_dir=contracts_tmp, capture_version=False)
    rendered = _render_table_rows(report)
    assert "adapters total" in rendered
    assert "reachable" in rendered
    assert "conform" in rendered


def test_render_list_rows_is_compact() -> None:
    """``list-status`` table omits version/notes columns."""
    from bernstein.cli.commands.adapters_cmd import _render_list_rows

    report = AdapterReport(
        adapters=(
            AdapterStatus("alpha", "alpha.py", None, None, frozenset(), CONFORMANCE_SKIP, "binary missing", "", ""),
        ),
        summary=ReportSummary(1, 0, 0, 0, 1),
    )
    rendered = _render_list_rows(report)
    assert "alpha" in rendered


def test_payload_is_dataclass_frozen() -> None:
    """``AdapterStatus`` is frozen so consumers can't mutate it in place."""
    s = AdapterStatus(
        name="x",
        module_path="x.py",
        binary_resolved=None,
        version_string=None,
        capabilities=frozenset(),
        conformance=CONFORMANCE_SKIP,
        conformance_detail="",
        last_modified_utc="",
        contract_hash="",
    )
    with pytest.raises((AttributeError, dataclasses.FrozenInstanceError)):
        s.name = "y"  # type: ignore[misc]


def test_conformance_verdict_payload_is_frozen() -> None:
    """``ConformanceVerdictPayload`` is frozen for safety in caching layers."""
    v = ConformanceVerdictPayload(verdict=CONFORMANCE_OK, detail="", capabilities=frozenset())
    with pytest.raises((AttributeError, dataclasses.FrozenInstanceError)):
        v.detail = "mutated"  # type: ignore[misc]


def test_capabilities_field_is_frozenset() -> None:
    """The capability set is a frozenset to prevent in-place mutation."""
    s = AdapterStatus(
        name="x",
        module_path="x.py",
        binary_resolved=None,
        version_string=None,
        capabilities=frozenset({"a"}),
        conformance=CONFORMANCE_SKIP,
        conformance_detail="",
        last_modified_utc="",
        contract_hash="",
    )
    assert isinstance(s.capabilities, frozenset)
