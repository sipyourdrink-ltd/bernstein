"""Tests for the ``fix_proof`` bug-fix quality gate (issue #6985)."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from bernstein.cli.commands.init_wizard_cmd import generate_yaml
from bernstein.cli.run_bootstrap import _generate_default_yaml
from bernstein.core.config.config_schema import BernsteinConfig
from bernstein.core.config.seed import parse_seed
from bernstein.core.planning.plan_loader import load_plan
from bernstein.core.planning.plan_schema import validate_plan
from bernstein.core.quality import fix_proof as fix_proof_mod
from bernstein.core.quality.fix_proof import (
    is_bug_fix_task,
    is_fix_proof_enabled,
    rerun_fix_proof_on_trees,
    run_fix_proof,
    verify_fix_proof_receipt,
)
from bernstein.core.quality.flaky_detector import FlakyDetector
from bernstein.core.quality.gate_pipeline import VALID_GATE_NAMES, GatePipelineStep
from bernstein.core.quality.gate_runner import GateRunner
from bernstein.core.quality.quality_gates import QualityGatesConfig
from bernstein.core.tasks.models import Complexity, Scope, Task, TaskType

if TYPE_CHECKING:
    from bernstein.core.quality.fix_proof import FixProofArmRecord


@pytest.fixture(autouse=True)
def _fast_pytest_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    # Avoid loading heavyweight unrelated pytest plugins in every throwaway arm subprocess.
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")


def _git(repo: Path, *args: str) -> str:
    res = subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout.strip()


def _init_bug_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    (repo / "src" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "src" / "calc.py").write_text(
        "def add_one(x: int) -> int:\n    return x  # bug\n\ndef double(x: int) -> int:\n    return x * 2\n",
        encoding="utf-8",
    )
    (repo / "tests" / "test_existing.py").write_text(
        "from src.calc import double\n\ndef test_double() -> None:\n    assert double(3) == 6\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base commit")
    return repo


def _fix_task(
    *,
    task_id: str = "T-412",
    role: str = "backend",
    task_type: TaskType = TaskType.FIX,
    title: str = "Fix #412: off-by-one in add_one",
    metadata: dict[str, object] | None = None,
) -> Task:
    return Task(
        id=task_id,
        title=title,
        description="Repair add_one calculation.",
        role=role,
        scope=Scope.SMALL,
        complexity=Complexity.LOW,
        task_type=task_type,
        metadata=metadata or {},
    )


class TestTaskClassificationAndEnablement:
    def test_is_bug_fix_task_matches_type_kind_and_issue_refs(self) -> None:
        assert is_bug_fix_task(_fix_task(task_type=TaskType.FIX, title="Fix calc"))
        assert is_bug_fix_task(_fix_task(task_type=TaskType.STANDARD, title="Calc", metadata={"kind": "bug"}))
        assert is_bug_fix_task(_fix_task(task_type=TaskType.STANDARD, title="Calc", metadata={"fixes_issue": 99}))
        assert is_bug_fix_task(_fix_task(task_type=TaskType.STANDARD, title="Fixes #42 in parser"))
        assert not is_bug_fix_task(_fix_task(task_type=TaskType.STANDARD, title="Add metrics dashboard"))

    def test_is_fix_proof_enabled_precedence(self) -> None:
        cfg = QualityGatesConfig(fix_proof=False, fix_proof_roles=["qa"])
        assert not is_fix_proof_enabled(cfg, _fix_task(role="backend"))
        assert is_fix_proof_enabled(cfg, _fix_task(role="qa"))
        assert is_fix_proof_enabled(cfg, _fix_task(role="backend", metadata={"fix_proof": True}))
        cfg_on = QualityGatesConfig(fix_proof=True)
        assert not is_fix_proof_enabled(cfg_on, _fix_task(metadata={"fix_proof": False}))

    def test_newly_generated_configs_enable_fix_proof_by_default(self, tmp_path: Path) -> None:
        wizard_yaml = generate_yaml(
            goal="Goal",
            project_type="python",
            max_agents=2,
            budget=5.0,
            adapter="auto",
            approval="auto",
        )
        default_yaml = _generate_default_yaml("python")
        for idx, body in enumerate((wizard_yaml, f'goal: "Goal"\n{default_yaml}')):
            assert "fix_proof: true" in body
            p = tmp_path / f"seed_{idx}.yaml"
            p.write_text(body, encoding="utf-8")
            seed = parse_seed(p)
            assert seed.quality_gates is not None
            assert seed.quality_gates.fix_proof is True
            cfg = BernsteinConfig(goal="Goal", quality_gates={"fix_proof": True})
            assert cfg.quality_gates is not None
            assert cfg.quality_gates.fix_proof is True

    def test_role_and_plan_configuration_parse_and_validate(self, tmp_path: Path) -> None:
        seed_path = tmp_path / "bernstein.yaml"
        seed_path.write_text(
            "goal: Test role enablement\nrole_model_policy:\n  qa:\n    model: claude-sonnet\n    fix_proof: true\n",
            encoding="utf-8",
        )
        seed = parse_seed(seed_path)
        assert seed.quality_gates is not None
        assert "qa" in seed.quality_gates.fix_proof_roles

        plan_raw = {
            "name": "bugfix-plan",
            "fix_proof": True,
            "stages": [
                {
                    "name": "repair",
                    "steps": [
                        {
                            "title": "Repair edge case",
                            "kind": "bug",
                            "fixes_issue": 412,
                        },
                        {
                            "title": "Opt out step",
                            "task_type": "fix",
                            "fix_proof": False,
                        },
                    ],
                }
            ],
        }
        assert validate_plan(plan_raw) == []
        plan_path = tmp_path / "plan.yaml"
        import yaml

        plan_path.write_text(yaml.safe_dump(plan_raw), encoding="utf-8")
        plan_cfg, tasks = load_plan(plan_path)
        assert plan_cfg.fix_proof is True
        assert tasks[0].task_type == TaskType.FIX
        assert tasks[0].metadata["fix_proof"] is True
        assert tasks[1].metadata["fix_proof"] is False


class TestFixProofExecutionAndOfflineVerification:
    def test_valid_bug_fix_passes_and_verifies_offline_across_rebase(self, tmp_path: Path) -> None:
        repo = _init_bug_repo(tmp_path)
        (repo / "src" / "calc.py").write_text(
            "def add_one(x: int) -> int:\n    return x + 1\n\ndef double(x: int) -> int:\n    return x * 2\n",
            encoding="utf-8",
        )
        (repo / "tests" / "test_bug_412.py").write_text(
            "from src.calc import add_one\n\n"
            "def test_add_one_fixes_off_by_one() -> None:\n"
            "    assert add_one(4) == 5\n",
            encoding="utf-8",
        )
        task = _fix_task()
        hmac_key = b"k" * 32
        receipt = run_fix_proof(
            task=task,
            workdir=repo,
            run_dir=repo,
            base_ref="HEAD",
            repeats=2,
            hmac_key=hmac_key,
        )

        assert receipt.verdict == "pass"
        assert receipt.proof_node_ids == ("tests/test_bug_412.py::test_add_one_fixes_off_by_one",)
        assert receipt.regression_node_ids == ("tests/test_existing.py::test_double",)
        assert receipt.proof_failure_ids == receipt.proof_node_ids
        assert receipt.regression_ids == ()
        assert receipt.arms["tests_on_base"].exit_statuses == (1, 1)
        assert receipt.arms["full_change"].exit_statuses == (0, 0)
        assert receipt.arms["source_reverted"].exit_statuses == (1, 1)
        assert receipt.arms["regression_base"].exit_statuses == (0, 0)
        assert receipt.arms["regression_full"].exit_statuses == (0, 0)
        assert all(d >= 0 for arm in receipt.arms.values() for d in arm.durations_ms)
        assert set(receipt.trees) == {"tests_on_base", "full_change", "source_reverted"}
        assert receipt.lineage_entry_hash

        # Offline cryptographic + evaluation verification passes cleanly.
        ok, errors = verify_fix_proof_receipt(receipt, lineage_root=repo / ".sdd" / "lineage", hmac_key=hmac_key)
        assert ok, errors

        # Tampering with receipt fails offline verification.
        tampered = replace(receipt, proof_failure_ids=())
        bad_ok, bad_errors = verify_fix_proof_receipt(
            tampered, lineage_root=repo / ".sdd" / "lineage", hmac_key=hmac_key
        )
        assert not bad_ok
        assert bad_errors

        # Simulate a rebase moving HEAD to an unrelated commit while keeping git tree objects intact.
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "fix 412")
        _git(repo, "checkout", "--orphan", "rebased-tip")
        (repo / "REBASE_MARKER.txt").write_text("rebased", encoding="utf-8")
        _git(repo, "add", "REBASE_MARKER.txt")
        _git(repo, "commit", "-m", "rebased commit")

        rerun = rerun_fix_proof_on_trees(repo, receipt, hmac_key=hmac_key)
        assert rerun.verdict_fields() == receipt.verdict_fields()

    def test_refuses_when_arm1_passes_bug_not_reproduced(self, tmp_path: Path) -> None:
        repo = _init_bug_repo(tmp_path)
        (repo / "src" / "calc.py").write_text(
            "def add_one(x: int) -> int:\n    return x + 1\n\ndef double(x: int) -> int:\n    return x * 2\n",
            encoding="utf-8",
        )
        # Test only checks double(), which already passed on base.
        (repo / "tests" / "test_bug_412.py").write_text(
            "from src.calc import double\n\ndef test_already_green_on_base() -> None:\n    assert double(2) == 4\n",
            encoding="utf-8",
        )
        receipt = run_fix_proof(
            task=_fix_task(), workdir=repo, run_dir=repo, base_ref="HEAD", repeats=2, hmac_key=b"a" * 32
        )
        assert receipt.verdict == "fail"
        assert "do not reproduce" in receipt.reason
        assert receipt.arms["tests_on_base"].exit_statuses == (0, 0)

    def test_refuses_when_arm3_passes_fix_not_exercised(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        repo = _init_bug_repo(tmp_path)
        (repo / "src" / "calc.py").write_text(
            "def add_one(x: int) -> int:\n    return x + 1\n\ndef double(x: int) -> int:\n    return x * 2\n",
            encoding="utf-8",
        )
        (repo / "tests" / "test_bug_412.py").write_text(
            "from src.calc import add_one\n\ndef test_add_one() -> None:\n    assert add_one(4) == 5\n",
            encoding="utf-8",
        )

        real_run_arm = fix_proof_mod._run_repeated_arm

        def _fake_run_arm(
            worktree: Path,
            *,
            arm: str,
            tree_id: str,
            python_exe: str,
            base_args: list[str],
            node_ids: tuple[str, ...],
            repeats: int,
            timeout_s: int,
            tmp_dir: Path,
        ) -> tuple[FixProofArmRecord, str | None, str | None]:
            rec, err_code, err_detail = real_run_arm(
                worktree,
                arm=arm,  # type: ignore[arg-type]
                tree_id=tree_id,
                python_exe=python_exe,
                base_args=base_args,
                node_ids=node_ids,
                repeats=repeats,
                timeout_s=timeout_s,
                tmp_dir=tmp_dir,
            )
            if arm == "source_reverted":
                # Simulate an arm-3 pass despite arm 1 failing.
                green = replace(
                    rec.runs[0], exit_code=0, phases=tuple(replace(p, outcome="passed") for p in rec.runs[0].phases)
                )
                return replace(rec, runs=(green, green)), None, None
            return rec, err_code, err_detail

        monkeypatch.setattr(fix_proof_mod, "_run_repeated_arm", _fake_run_arm)
        receipt = run_fix_proof(
            task=_fix_task(), workdir=repo, run_dir=repo, base_ref="HEAD", repeats=2, hmac_key=b"b" * 32
        )
        assert receipt.verdict == "fail"
        assert "do not exercise" in receipt.reason

    def test_refuses_arm4_regression_and_records_test_id(self, tmp_path: Path) -> None:
        repo = _init_bug_repo(tmp_path)
        # Fix add_one, but accidentally break double().
        (repo / "src" / "calc.py").write_text(
            "def add_one(x: int) -> int:\n    return x + 1\n\ndef double(x: int) -> int:\n    return x * 3\n",
            encoding="utf-8",
        )
        (repo / "tests" / "test_bug_412.py").write_text(
            "from src.calc import add_one\n\ndef test_add_one() -> None:\n    assert add_one(4) == 5\n",
            encoding="utf-8",
        )
        receipt = run_fix_proof(
            task=_fix_task(), workdir=repo, run_dir=repo, base_ref="HEAD", repeats=2, hmac_key=b"c" * 32
        )
        assert receipt.verdict == "fail"
        assert receipt.regression_ids == ("tests/test_existing.py::test_double",)

    def test_error_only_arm_yields_inconclusive(self, tmp_path: Path) -> None:
        repo = _init_bug_repo(tmp_path)
        # Agent adds a new helper and the test imports it at call time; on base tree it raises ImportError.
        (repo / "src" / "calc.py").write_text(
            "def add_one(x: int) -> int:\n"
            "    return x + 1\n\n"
            "def double(x: int) -> int:\n"
            "    return x * 2\n\n"
            "def new_helper() -> int:\n"
            "    return 7\n",
            encoding="utf-8",
        )
        (repo / "tests" / "test_bug_412.py").write_text(
            "def test_imports_added_symbol() -> None:\n"
            "    from src.calc import new_helper\n"
            "    assert new_helper() == 7\n",
            encoding="utf-8",
        )
        receipt = run_fix_proof(
            task=_fix_task(), workdir=repo, run_dir=repo, base_ref="HEAD", repeats=2, hmac_key=b"d" * 32
        )
        assert receipt.verdict == "inconclusive"

    def test_unstable_repeats_yield_inconclusive(self, tmp_path: Path) -> None:
        repo = _init_bug_repo(tmp_path)
        marker = tmp_path / "flip_state.txt"
        (repo / "src" / "calc.py").write_text(
            "def add_one(x: int) -> int:\n    return x + 1\n\ndef double(x: int) -> int:\n    return x * 2\n",
            encoding="utf-8",
        )
        (repo / "tests" / "test_bug_412.py").write_text(
            "from pathlib import Path\n"
            "from src.calc import add_one\n\n"
            "def test_flaky_across_repeats() -> None:\n"
            f"    state = Path({str(marker)!r})\n"
            "    count = int(state.read_text()) if state.exists() else 0\n"
            "    state.write_text(str(count + 1))\n"
            "    if count % 2 == 1:\n"
            "        assert add_one(4) == 5\n"
            "    else:\n"
            "        assert add_one(4) == 999\n",
            encoding="utf-8",
        )
        receipt = run_fix_proof(
            task=_fix_task(), workdir=repo, run_dir=repo, base_ref="HEAD", repeats=2, hmac_key=b"e" * 32
        )
        assert receipt.verdict == "inconclusive"

    def test_quarantined_proof_test_refused_and_preserves_initial_selection(self, tmp_path: Path) -> None:
        repo = _init_bug_repo(tmp_path)
        (repo / "src" / "calc.py").write_text(
            "def add_one(x: int) -> int:\n    return x + 1\n\ndef double(x: int) -> int:\n    return x * 2\n",
            encoding="utf-8",
        )
        (repo / "tests" / "test_bug_412.py").write_text(
            "from src.calc import add_one\n\ndef test_add_one() -> None:\n    assert add_one(4) == 5\n",
            encoding="utf-8",
        )
        quarantine_file = repo / FlakyDetector.QUARANTINE_FILE
        quarantine_file.parent.mkdir(parents=True, exist_ok=True)
        quarantine_file.write_text(json.dumps(["tests/test_bug_412.py::test_add_one"]), encoding="utf-8")

        receipt = run_fix_proof(
            task=_fix_task(), workdir=repo, run_dir=repo, base_ref="HEAD", repeats=2, hmac_key=b"f" * 32
        )
        assert receipt.verdict == "inconclusive"
        assert receipt.proof_node_ids == ("tests/test_bug_412.py::test_add_one",)

    @pytest.mark.asyncio
    async def test_gate_runner_integrates_fix_proof_for_bug_and_skips_feature(self, tmp_path: Path) -> None:
        assert "fix_proof" in VALID_GATE_NAMES
        repo = _init_bug_repo(tmp_path)
        base_sha = _git(repo, "rev-parse", "HEAD")
        (repo / "src" / "calc.py").write_text(
            "def add_one(x: int) -> int:\n    return x + 1\n\ndef double(x: int) -> int:\n    return x * 2\n",
            encoding="utf-8",
        )
        (repo / "tests" / "test_bug_412.py").write_text(
            "from src.calc import add_one\n\ndef test_add_one() -> None:\n    assert add_one(4) == 5\n",
            encoding="utf-8",
        )
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "fix 412")
        cfg = QualityGatesConfig(
            enabled=True,
            lint=False,
            type_check=False,
            tests=False,
            pii_scan=False,
            dlp_scan=False,
            run_config=False,
            fix_proof=False,
            fix_proof_roles=["qa"],
        )
        runner = GateRunner(cfg, repo, base_ref=base_sha)
        report_bug = await runner.run_all(_fix_task(role="qa"), repo)
        assert report_bug.overall_pass is True
        fp_result = next(r for r in report_bug.results if r.name == "fix_proof")
        assert fp_result.status == "pass"
        assert fp_result.metadata["proof_node_ids"] == ["tests/test_bug_412.py::test_add_one"]

        # Feature task should skip even if pipeline explicitly contains fix_proof.
        explicit_cfg = QualityGatesConfig(
            enabled=True,
            lint=False,
            type_check=False,
            tests=False,
            pii_scan=False,
            dlp_scan=False,
            run_config=False,
            pipeline=[GatePipelineStep(name="fix_proof", required=True, condition="any_changed")],
        )
        feature_task = _fix_task(task_type=TaskType.STANDARD, title="Add new logo")
        report_feature = await GateRunner(explicit_cfg, repo, base_ref=base_sha).run_all(feature_task, repo)
        assert report_feature.results[0].status == "skipped"
