"""CLI tests for ``bernstein eval ab`` suite mode (issue #2247).

Acceptance criterion 3 at the CLI seam: the full three-arm flow runs
with the synthetic executor - zero network, deterministic verdicts, a
real (synthetic) spend ledger, a content-addressed artifact, and a
verified audit chain event.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.eval_benchmark_cmd import eval_group
from bernstein.core.security.audit import AuditLog
from bernstein.core.security.audit_chain import EVENT_EVAL_AB_COMPARISON
from bernstein.eval.ab_comparison import ARM_CANDIDATE, INCOMPARABLE

_SUITE_YAML = (
    "tasks:\n"
    "  - id: t1\n    input: hello\n    expected: candidate::hello\n"
    "  - id: t2\n    input: world\n    expected: candidate::world\n"
)

_TWO_ARM_SUITE_YAML = (
    "tasks:\n"
    "  - id: t1\n    input: hello\n    expected: terse::hello\n"
    "  - id: t2\n    input: world\n    expected: terse::world\n"
)


def _invoke_suite(runner: CliRunner, *extra: str, suite_yaml: str = _SUITE_YAML) -> object:
    Path("suite.yaml").write_text(suite_yaml, encoding="utf-8")
    return runner.invoke(
        eval_group,
        ["ab", "--suite", "suite.yaml", *extra],
        env={"BERNSTEIN_AUDIT_KEY_PATH": str(Path("keys") / "audit.key")},
    )


class TestSuiteModeThreeArm:
    def test_full_three_arm_flow_offline(self) -> None:
        """AC3: three arms, two trials, synthetic executor, zero network."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = _invoke_suite(
                runner,
                "--arm-a",
                "baseline",
                "--arm-b",
                "terse",
                "--arms",
                "3",
                "--trials",
                "2",
                "--json",
            )
            assert result.exit_code == 0, result.output
            payload = json.loads(result.output)
            content = payload["content"]

            # 3 arms x 2 tasks x 2 trials.
            assert len(content["per_task"]) == 12
            # Candidate passes every run (expected matches its output);
            # control and baseline fail; costs are ledger-resolved.
            assert content["winner"]["arm"] == ARM_CANDIDATE
            assert all(row["ledger_ref"] for row in content["per_task"])

            # The artifact is on disk, content-addressed.
            artifact = Path(payload["artifact"])
            assert artifact.exists()
            assert artifact.name == f"{payload['sha256']}.json"

            # The synthetic ledger exists at its dedicated default path.
            assert Path(".sdd/eval/ab/ledger.jsonl").exists()

            # The chain event landed and the chain verifies.
            log = AuditLog(audit_dir=Path(".sdd/audit"), key_path=Path("keys") / "audit.key")
            events = log.query(event_type=EVENT_EVAL_AB_COMPARISON)
            assert len(events) == 1
            assert events[0].details["artifact_sha256"] == payload["sha256"]
            ok, errors = log.verify()
            assert ok, errors

            # The pair index links the honest named-profile pair.
            index = Path(".sdd/reports/eval_ab") / "index.jsonl"
            row = json.loads(index.read_text(encoding="utf-8").splitlines()[-1])
            assert (row["profile_a"], row["profile_b"]) == ("balanced", "terse")
            assert row["artifact_sha256"] == payload["sha256"]

    def test_incomparable_when_suite_has_no_expectations(self) -> None:
        """AC4 at the CLI seam: no verdicts -> incomparable, exit 0."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = _invoke_suite(
                runner,
                "--arm-a",
                "baseline",
                "--arm-b",
                "terse",
                "--arms",
                "3",
                "--json",
                suite_yaml="tasks:\n  - id: t1\n    input: hello\n",
            )
            assert result.exit_code == 0, result.output
            content = json.loads(result.output)["content"]
            assert content["winner"]["arm"] == INCOMPARABLE
            assert content["winner"]["missing"]

    def test_three_arm_rejects_named_profile_baseline(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = _invoke_suite(runner, "--arm-a", "terse", "--arm-b", "verbose", "--arms", "3")
            assert result.exit_code == 2, result.output
            assert "baseline" in result.output


class TestSuiteModeTwoArm:
    def test_two_arm_default(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = _invoke_suite(
                runner,
                "--arm-a",
                "balanced",
                "--arm-b",
                "terse",
                "--json",
                suite_yaml=_TWO_ARM_SUITE_YAML,
            )
            assert result.exit_code == 0, result.output
            content = json.loads(result.output)["content"]
            assert set(content["arms"]) == {"balanced", "terse"}
            assert content["winner"]["arm"] == "terse"
            assert list(content["deltas"]) == ["terse_vs_balanced"]

    def test_human_output_mentions_winner_and_chain(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = _invoke_suite(
                runner,
                "--arm-a",
                "balanced",
                "--arm-b",
                "terse",
                suite_yaml=_TWO_ARM_SUITE_YAML,
            )
            assert result.exit_code == 0, result.output
            assert "Winner:" in result.output
            assert "eval.ab_comparison" in result.output

    def test_output_file_written(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = _invoke_suite(
                runner,
                "--arm-a",
                "balanced",
                "--arm-b",
                "terse",
                "--output",
                "out.json",
                suite_yaml=_TWO_ARM_SUITE_YAML,
            )
            assert result.exit_code == 0, result.output
            written = json.loads(Path("out.json").read_text(encoding="utf-8"))
            assert written["content"]["winner"]["arm"] == "terse"


class TestSuiteModeValidation:
    def test_suite_requires_both_arms(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            Path("suite.yaml").write_text(_SUITE_YAML, encoding="utf-8")
            result = runner.invoke(eval_group, ["ab", "--suite", "suite.yaml"])
        assert result.exit_code == 2, result.output
        assert "--arm-a" in result.output

    def test_bare_invocation_mentions_both_modes(self) -> None:
        runner = CliRunner()
        result = runner.invoke(eval_group, ["ab"])
        assert result.exit_code == 2, result.output
        assert "--variant-a" in result.output
        assert "--suite" in result.output

    @pytest.mark.parametrize("arms", ["1", "4"])
    def test_arm_count_out_of_range(self, arms: str) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = _invoke_suite(runner, "--arm-a", "balanced", "--arm-b", "terse", "--arms", arms)
        assert result.exit_code == 2, result.output

    def test_unknown_profile_is_usage_error(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = _invoke_suite(runner, "--arm-a", "balanced", "--arm-b", "bogus")
        assert result.exit_code == 2, result.output
        assert "unknown response style" in result.output
