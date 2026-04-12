"""Tests for FileUpgradeExecutor — YAML read-modify-write and rollback."""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import yaml

from bernstein.core.evolution import (
    AnalysisTrigger,
    ApprovalMode,
    FileUpgradeExecutor,
    UpgradeCategory,
    UpgradeProposal,
)
from bernstein.core.models import RiskAssessment, RollbackPlan, Task, TaskStatus, TaskType
from bernstein.core.upgrade_executor import (
    FileChange,
    UpgradeExecutor,
    UpgradeReviewer,
    UpgradeTransaction,
    UpgradeType,
    create_upgrade_from_task,
)
from bernstein.core.upgrade_executor import (
    UpgradeStatus as ExecutorUpgradeStatus,
)


def _make_proposal(
    category: UpgradeCategory,
    proposal_id: str = "TEST-001",
    title: str = "Test Upgrade",
    proposed_change: str = "Test change description",
) -> UpgradeProposal:
    """Create a minimal UpgradeProposal for testing."""
    return UpgradeProposal(
        id=proposal_id,
        title=title,
        category=category,
        description="Test upgrade description",
        current_state="current state",
        proposed_change=proposed_change,
        benefits=["benefit 1"],
        risk_assessment=RiskAssessment(level="low"),
        rollback_plan=RollbackPlan(steps=["Revert changes"], estimated_rollback_minutes=5),
        cost_estimate_usd=0.0,
        expected_improvement="10% improvement",
        confidence=0.8,
        approval_mode=ApprovalMode.AUTO,
        triggered_by=AnalysisTrigger.SCHEDULED,
    )


class TestFileUpgradeExecutorPolicyUpdate:
    """Tests for _apply_policy_update."""

    def test_creates_policies_yaml_if_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = FileUpgradeExecutor(Path(tmpdir))
            proposal = _make_proposal(UpgradeCategory.POLICY_UPDATE)

            result = executor.execute_upgrade(proposal)

            assert result is True
            config_file = Path(tmpdir) / "config" / "policies.yaml"
            assert config_file.exists()

    def test_writes_pending_upgrade_to_policies_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = FileUpgradeExecutor(Path(tmpdir))
            proposal = _make_proposal(
                UpgradeCategory.POLICY_UPDATE,
                proposal_id="POL-001",
                title="Optimize free tier routing",
                proposed_change="Route more tasks to free tier",
            )

            executor.execute_upgrade(proposal)

            config_file = Path(tmpdir) / "config" / "policies.yaml"
            with config_file.open() as f:
                data = yaml.safe_load(f)

            assert "pending_upgrades" in data
            entry = data["pending_upgrades"][0]
            assert entry["id"] == "POL-001"
            assert entry["title"] == "Optimize free tier routing"
            assert entry["change"] == "Route more tasks to free tier"

    def test_preserves_existing_policies_yaml_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / "config"
            config_dir.mkdir(parents=True)
            existing_policies: dict[str, Any] = {"policies": [{"id": "existing-policy", "name": "Existing"}]}
            with (config_dir / "policies.yaml").open("w") as f:
                yaml.dump(existing_policies, f)

            executor = FileUpgradeExecutor(Path(tmpdir))
            executor.execute_upgrade(_make_proposal(UpgradeCategory.POLICY_UPDATE))

            with (config_dir / "policies.yaml").open() as f:
                data = yaml.safe_load(f)

            # Original content preserved
            assert data["policies"][0]["id"] == "existing-policy"
            # Upgrade appended
            assert len(data["pending_upgrades"]) == 1

    def test_records_to_history_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = FileUpgradeExecutor(Path(tmpdir))
            proposal = _make_proposal(UpgradeCategory.POLICY_UPDATE, proposal_id="POL-002")

            executor.execute_upgrade(proposal)

            history_file = Path(tmpdir) / "upgrades" / "history.jsonl"
            assert history_file.exists()
            lines = [json.loads(l) for l in history_file.read_text().splitlines() if l.strip()]
            assert any(entry["proposal_id"] == "POL-002" for entry in lines)
            assert any(entry["status"] == "applied" for entry in lines)


class TestFileUpgradeExecutorRoutingRules:
    """Tests for _apply_routing_rules and _apply_model_routing."""

    def test_creates_routing_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = FileUpgradeExecutor(Path(tmpdir))
            result = executor.execute_upgrade(_make_proposal(UpgradeCategory.ROUTING_RULES))

            assert result is True
            assert (Path(tmpdir) / "config" / "routing.yaml").exists()

    def test_model_routing_writes_to_routing_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = FileUpgradeExecutor(Path(tmpdir))
            result = executor.execute_upgrade(_make_proposal(UpgradeCategory.MODEL_ROUTING))

            assert result is True
            assert (Path(tmpdir) / "config" / "routing.yaml").exists()

    def test_multiple_routing_upgrades_accumulate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = FileUpgradeExecutor(Path(tmpdir))

            for i in range(3):
                executor.execute_upgrade(_make_proposal(UpgradeCategory.ROUTING_RULES, proposal_id=f"RT-{i:03d}"))

            config_file = Path(tmpdir) / "config" / "routing.yaml"
            with config_file.open() as f:
                data = yaml.safe_load(f)

            assert len(data["pending_upgrades"]) == 3


class TestFileUpgradeExecutorProviderConfig:
    """Tests for _apply_provider_config."""

    def test_creates_providers_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = FileUpgradeExecutor(Path(tmpdir))
            result = executor.execute_upgrade(_make_proposal(UpgradeCategory.PROVIDER_CONFIG))

            assert result is True
            assert (Path(tmpdir) / "config" / "providers.yaml").exists()

    def test_preserves_existing_provider_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / "config"
            config_dir.mkdir(parents=True)
            existing: dict[str, Any] = {"providers": {"openrouter_free": {"tier": "free", "cost_per_1k_tokens": 0.0}}}
            with (config_dir / "providers.yaml").open("w") as f:
                yaml.dump(existing, f)

            executor = FileUpgradeExecutor(Path(tmpdir))
            executor.execute_upgrade(_make_proposal(UpgradeCategory.PROVIDER_CONFIG))

            with (config_dir / "providers.yaml").open() as f:
                data = yaml.safe_load(f)

            assert "openrouter_free" in data["providers"]
            assert len(data["pending_upgrades"]) == 1


class TestFileUpgradeExecutorRoleTemplate:
    """Tests for _apply_role_template."""

    def test_creates_proposed_upgrades_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            # Point state_dir to tmpdir/.sdd so templates land in tmpdir/templates
            sdd_dir = Path(tmpdir) / ".sdd"
            sdd_dir.mkdir(parents=True)
            executor = FileUpgradeExecutor(sdd_dir)
            proposal = _make_proposal(UpgradeCategory.ROLE_TEMPLATES, proposal_id="TPL-001")

            result = executor.execute_upgrade(proposal)

            assert result is True
            proposals_file = Path(tmpdir) / "templates" / "roles" / "PROPOSED_UPGRADES.jsonl"
            assert proposals_file.exists()
            lines = [json.loads(l) for l in proposals_file.read_text().splitlines() if l.strip()]
            assert lines[0]["id"] == "TPL-001"


class TestFileUpgradeExecutorAtomicWrite:
    """Tests that file writes are atomic (no partial writes)."""

    def test_no_tmp_file_left_after_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = FileUpgradeExecutor(Path(tmpdir))
            executor.execute_upgrade(_make_proposal(UpgradeCategory.POLICY_UPDATE))

            config_dir = Path(tmpdir) / "config"
            tmp_files = list(config_dir.glob("*.tmp"))
            assert len(tmp_files) == 0


class TestFileUpgradeExecutorRollback:
    """Tests for rollback capability."""

    def test_rollback_restores_original_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / "config"
            config_dir.mkdir(parents=True)
            original: dict[str, Any] = {"policies": [{"id": "original"}]}
            with (config_dir / "policies.yaml").open("w") as f:
                yaml.dump(original, f)

            executor = FileUpgradeExecutor(Path(tmpdir))
            proposal = _make_proposal(UpgradeCategory.POLICY_UPDATE)
            executor.execute_upgrade(proposal)

            # File is now modified
            with (config_dir / "policies.yaml").open() as f:
                modified = yaml.safe_load(f)
            assert "pending_upgrades" in modified

            # Rollback
            result = executor.rollback_upgrade(proposal)
            assert result is True

            # File is restored
            with (config_dir / "policies.yaml").open() as f:
                restored = yaml.safe_load(f)
            assert restored == original

    def test_rollback_without_backup_succeeds(self) -> None:
        """Rollback when no file existed before should succeed without error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = FileUpgradeExecutor(Path(tmpdir))
            # No backup taken (file didn't exist before)
            proposal = _make_proposal(UpgradeCategory.POLICY_UPDATE)
            executor.execute_upgrade(proposal)

            # Manually clear backup to simulate "no backup"
            executor._backup_files.clear()

            result = executor.rollback_upgrade(proposal)
            assert result is True


# ---------------------------------------------------------------------------
# Tests for bernstein.core.upgrade_executor (UpgradeExecutor class)
# ---------------------------------------------------------------------------


def _make_executor(tmp_path: Path, auto_git: bool = False) -> UpgradeExecutor:
    """Create an UpgradeExecutor with git disabled by default to avoid real git calls."""
    return UpgradeExecutor(workdir=tmp_path, auto_git=auto_git)


def _make_transaction(
    upgrade_type: UpgradeType = UpgradeType.CODE_MODIFICATION,
    title: str = "Test Transaction",
    description: str = "A test upgrade",
    file_changes: list[FileChange] | None = None,
) -> UpgradeTransaction:
    """Create a minimal UpgradeTransaction."""
    return UpgradeTransaction(
        id="txn-test-001",
        upgrade_type=upgrade_type,
        title=title,
        description=description,
        file_changes=file_changes or [],
    )


class TestUpgradeTransactionCreation:
    """UpgradeTransaction dataclass construction and default values."""

    def test_default_status_is_pending_review(self) -> None:
        txn = _make_transaction()
        assert txn.status == ExecutorUpgradeStatus.PENDING_REVIEW

    def test_created_at_is_set_on_construction(self) -> None:
        before = time.time()
        txn = _make_transaction()
        after = time.time()
        assert before <= txn.created_at <= after

    def test_optional_fields_default_to_none(self) -> None:
        txn = _make_transaction()
        assert txn.executed_at is None
        assert txn.completed_at is None
        assert txn.git_commit is None
        assert txn.task_id is None

    def test_file_changes_default_to_empty_list(self) -> None:
        txn = _make_transaction()
        assert txn.file_changes == []

    def test_all_upgrade_type_values(self) -> None:
        for upgrade_type in UpgradeType:
            txn = _make_transaction(upgrade_type=upgrade_type)
            assert txn.upgrade_type == upgrade_type


class TestUpgradeTransactionStatusTransitions:
    """Manually driving status through the UpgradeStatus enum."""

    def test_transition_to_executing(self) -> None:
        txn = _make_transaction()
        txn.status = ExecutorUpgradeStatus.REVIEW_APPROVED
        txn.status = ExecutorUpgradeStatus.EXECUTING
        assert txn.status == ExecutorUpgradeStatus.EXECUTING

    def test_transition_to_completed(self) -> None:
        txn = _make_transaction()
        txn.status = ExecutorUpgradeStatus.COMPLETED
        assert txn.status == ExecutorUpgradeStatus.COMPLETED

    def test_transition_to_failed(self) -> None:
        txn = _make_transaction()
        txn.status = ExecutorUpgradeStatus.FAILED
        assert txn.status == ExecutorUpgradeStatus.FAILED

    def test_transition_to_rolled_back(self) -> None:
        txn = _make_transaction()
        txn.status = ExecutorUpgradeStatus.ROLLED_BACK
        assert txn.status == ExecutorUpgradeStatus.ROLLED_BACK

    def test_all_statuses_have_string_value(self) -> None:
        for status in ExecutorUpgradeStatus:
            assert isinstance(status.value, str)


class TestFileChangeOperations:
    """FileChange dataclass stores operation metadata correctly."""

    def test_create_operation(self) -> None:
        fc = FileChange(path="src/new_file.py", operation="create", new_content="# new\n")
        assert fc.operation == "create"
        assert fc.new_content == "# new\n"
        assert fc.old_content is None
        assert fc.backup_path is None

    def test_modify_operation(self) -> None:
        fc = FileChange(
            path="src/existing.py",
            operation="modify",
            old_content="old",
            new_content="new",
        )
        assert fc.operation == "modify"
        assert fc.old_content == "old"
        assert fc.new_content == "new"

    def test_delete_operation(self) -> None:
        fc = FileChange(path="src/obsolete.py", operation="delete", old_content="# old file")
        assert fc.operation == "delete"
        assert fc.old_content == "# old file"
        assert fc.new_content is None


class TestUpgradeExecutorCreateBackups:
    """_create_backups creates backup copies for modify/delete, skips create."""

    def test_backup_created_for_modify(self, tmp_path: Path) -> None:
        target = tmp_path / "target.py"
        target.write_text("original content")
        executor = _make_executor(tmp_path)
        txn = _make_transaction(file_changes=[FileChange(path="target.py", operation="modify", new_content="new")])
        executor._create_backups(txn)
        assert txn.file_changes[0].backup_path is not None
        assert Path(txn.file_changes[0].backup_path).read_text() == "original content"

    def test_backup_created_for_delete(self, tmp_path: Path) -> None:
        target = tmp_path / "delete_me.py"
        target.write_text("to be deleted")
        executor = _make_executor(tmp_path)
        txn = _make_transaction(file_changes=[FileChange(path="delete_me.py", operation="delete")])
        executor._create_backups(txn)
        assert txn.file_changes[0].backup_path is not None

    def test_no_backup_for_create(self, tmp_path: Path) -> None:
        executor = _make_executor(tmp_path)
        txn = _make_transaction(file_changes=[FileChange(path="new_file.py", operation="create", new_content="x")])
        executor._create_backups(txn)
        assert txn.file_changes[0].backup_path is None

    def test_backup_skipped_when_file_missing(self, tmp_path: Path) -> None:
        executor = _make_executor(tmp_path)
        txn = _make_transaction(file_changes=[FileChange(path="nonexistent.py", operation="modify", new_content="x")])
        executor._create_backups(txn)
        # No backup since the source file doesn't exist
        assert txn.file_changes[0].backup_path is None


class TestUpgradeExecutorApplyChanges:
    """_apply_changes handles create, modify, and delete correctly."""

    def test_create_writes_new_file(self, tmp_path: Path) -> None:
        executor = _make_executor(tmp_path)
        txn = _make_transaction(file_changes=[FileChange(path="sub/new.py", operation="create", new_content="hello")])
        executor._apply_changes(txn)
        assert (tmp_path / "sub" / "new.py").read_text() == "hello"

    def test_create_with_empty_content(self, tmp_path: Path) -> None:
        executor = _make_executor(tmp_path)
        txn = _make_transaction(file_changes=[FileChange(path="empty.py", operation="create")])
        executor._apply_changes(txn)
        assert (tmp_path / "empty.py").exists()
        assert (tmp_path / "empty.py").read_text() == ""

    def test_modify_overwrites_file(self, tmp_path: Path) -> None:
        target = tmp_path / "mod.py"
        target.write_text("old content")
        executor = _make_executor(tmp_path)
        txn = _make_transaction(file_changes=[FileChange(path="mod.py", operation="modify", new_content="new content")])
        executor._apply_changes(txn)
        assert target.read_text() == "new content"

    def test_modify_with_none_content_skips_write(self, tmp_path: Path) -> None:
        target = tmp_path / "unchanged.py"
        target.write_text("original")
        executor = _make_executor(tmp_path)
        txn = _make_transaction(file_changes=[FileChange(path="unchanged.py", operation="modify", new_content=None)])
        executor._apply_changes(txn)
        assert target.read_text() == "original"

    def test_delete_removes_file(self, tmp_path: Path) -> None:
        target = tmp_path / "bye.py"
        target.write_text("goodbye")
        executor = _make_executor(tmp_path)
        txn = _make_transaction(file_changes=[FileChange(path="bye.py", operation="delete")])
        executor._apply_changes(txn)
        assert not target.exists()

    def test_delete_nonexistent_file_is_noop(self, tmp_path: Path) -> None:
        executor = _make_executor(tmp_path)
        txn = _make_transaction(file_changes=[FileChange(path="ghost.py", operation="delete")])
        executor._apply_changes(txn)  # Should not raise


class TestUpgradeExecutorVerifyChanges:
    """_verify_changes raises ValueError on integrity failures."""

    def test_verify_create_passes_when_file_exists(self, tmp_path: Path) -> None:
        target = tmp_path / "created.py"
        target.write_text("x")
        executor = _make_executor(tmp_path)
        txn = _make_transaction(file_changes=[FileChange(path="created.py", operation="create", new_content="x")])
        executor._verify_changes(txn)  # Should not raise

    def test_verify_create_raises_when_file_missing(self, tmp_path: Path) -> None:
        executor = _make_executor(tmp_path)
        txn = _make_transaction(file_changes=[FileChange(path="missing.py", operation="create", new_content="x")])
        with pytest.raises(ValueError, match="missing.py"):
            executor._verify_changes(txn)

    def test_verify_modify_raises_on_content_mismatch(self, tmp_path: Path) -> None:
        target = tmp_path / "mod.py"
        target.write_text("wrong content")
        executor = _make_executor(tmp_path)
        txn = _make_transaction(file_changes=[FileChange(path="mod.py", operation="modify", new_content="expected")])
        with pytest.raises(ValueError, match="content mismatch"):
            executor._verify_changes(txn)

    def test_verify_delete_raises_when_file_still_exists(self, tmp_path: Path) -> None:
        target = tmp_path / "zombie.py"
        target.write_text("still here")
        executor = _make_executor(tmp_path)
        txn = _make_transaction(file_changes=[FileChange(path="zombie.py", operation="delete")])
        with pytest.raises(ValueError, match="zombie.py"):
            executor._verify_changes(txn)


class TestUpgradeExecutorRollbackFromBackups:
    """_rollback_upgrade restores files from backups when no git commit."""

    def test_rollback_restores_backed_up_file(self, tmp_path: Path) -> None:
        target = tmp_path / "restored.py"
        target.write_text("new content — bad")
        executor = _make_executor(tmp_path)
        txn = _make_transaction(file_changes=[FileChange(path="restored.py", operation="modify", new_content="new")])
        # Create a manual backup
        backup = tmp_path / ".sdd" / "upgrades" / "backups" / "txn-test-001" / "restored.py"
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_text("original content")
        txn.file_changes[0].backup_path = str(backup)

        executor._rollback_upgrade(txn)

        assert txn.status == ExecutorUpgradeStatus.ROLLED_BACK
        assert txn.rolled_back_at is not None
        assert target.read_text() == "original content"

    def test_rollback_sets_status_even_without_backups(self, tmp_path: Path) -> None:
        executor = _make_executor(tmp_path)
        txn = _make_transaction()
        executor._rollback_upgrade(txn)
        assert txn.status == ExecutorUpgradeStatus.ROLLED_BACK


class TestUpgradeReviewer:
    """UpgradeReviewer calls call_llm and parses responses."""

    def test_approve_verdict_parsed_correctly(self, tmp_path: Path) -> None:
        reviewer = UpgradeReviewer(workdir=tmp_path)
        txn = _make_transaction(file_changes=[FileChange(path="x.py", operation="create", new_content="x")])
        approve_json = json.dumps(
            {
                "verdict": "approve",
                "reasoning": "Looks good",
                "feedback": "No issues",
                "risk_level": "low",
                "suggested_improvements": [],
            }
        )

        async def run() -> Any:
            with patch("bernstein.core.config.upgrade_executor.call_llm", new=AsyncMock(return_value=approve_json)):
                return await reviewer.review_upgrade(txn)

        result = asyncio.run(run())
        assert result.verdict == "approve"
        assert result.risk_level == "low"

    def test_reject_verdict_parsed_correctly(self, tmp_path: Path) -> None:
        reviewer = UpgradeReviewer(workdir=tmp_path)
        txn = _make_transaction()
        reject_json = json.dumps(
            {
                "verdict": "reject",
                "reasoning": "Too risky",
                "feedback": "Do not proceed",
            }
        )

        async def run() -> Any:
            with patch("bernstein.core.config.upgrade_executor.call_llm", new=AsyncMock(return_value=reject_json)):
                return await reviewer.review_upgrade(txn)

        result = asyncio.run(run())
        assert result.verdict == "reject"

    def test_llm_error_returns_request_changes(self, tmp_path: Path) -> None:
        reviewer = UpgradeReviewer(workdir=tmp_path)
        txn = _make_transaction()

        async def run() -> Any:
            with patch(
                "bernstein.core.upgrade_executor.call_llm",
                new=AsyncMock(side_effect=RuntimeError("network error")),
            ):
                return await reviewer.review_upgrade(txn)

        result = asyncio.run(run())
        assert result.verdict == "request_changes"
        assert "network error" in result.reasoning

    def test_malformed_json_falls_back_gracefully(self, tmp_path: Path) -> None:
        reviewer = UpgradeReviewer(workdir=tmp_path)
        txn = _make_transaction()

        async def run() -> Any:
            with patch(
                "bernstein.core.upgrade_executor.call_llm",
                new=AsyncMock(return_value="I approve of this change"),
            ):
                return await reviewer.review_upgrade(txn)

        result = asyncio.run(run())
        assert result.verdict == "approve"

    def test_markdown_wrapped_json_is_unwrapped(self, tmp_path: Path) -> None:
        reviewer = UpgradeReviewer(workdir=tmp_path)
        txn = _make_transaction()
        wrapped = '```json\n{"verdict": "approve", "reasoning": "ok", "feedback": "fine"}\n```'

        async def run() -> Any:
            with patch("bernstein.core.config.upgrade_executor.call_llm", new=AsyncMock(return_value=wrapped)):
                return await reviewer.review_upgrade(txn)

        result = asyncio.run(run())
        assert result.verdict == "approve"


class TestSubmitUpgradeFlow:
    """Full submit_upgrade flow with mocked reviewer."""

    def test_submit_upgrade_approved_and_completed(self, tmp_path: Path) -> None:
        executor = _make_executor(tmp_path, auto_git=False)

        target_rel = "sub/created.py"
        fc = FileChange(path=target_rel, operation="create", new_content="# created\n")

        approve_result = AsyncMock(return_value="approve")
        approve_result.verdict = "approve"

        async def run() -> UpgradeTransaction:
            mock_reviewer = AsyncMock()
            mock_reviewer.review_upgrade = AsyncMock(
                return_value=type(
                    "R",
                    (),
                    {
                        "verdict": "approve",
                        "feedback": "ok",
                        "reasoning": "good",
                    },
                )()
            )
            executor._reviewer = mock_reviewer
            return await executor.submit_upgrade(
                upgrade_type=UpgradeType.CODE_MODIFICATION,
                title="Add file",
                description="Creates a new source file",
                file_changes=[fc],
            )

        txn = asyncio.run(run())
        assert txn.status == ExecutorUpgradeStatus.COMPLETED
        assert (tmp_path / target_rel).exists()

    def test_submit_upgrade_rejected_stays_rejected(self, tmp_path: Path) -> None:
        executor = _make_executor(tmp_path, auto_git=False)

        async def run() -> UpgradeTransaction:
            mock_reviewer = AsyncMock()
            mock_reviewer.review_upgrade = AsyncMock(
                return_value=type(
                    "R",
                    (),
                    {
                        "verdict": "reject",
                        "feedback": "Nope",
                        "reasoning": "Bad idea",
                    },
                )()
            )
            executor._reviewer = mock_reviewer
            return await executor.submit_upgrade(
                upgrade_type=UpgradeType.CONFIG_ADJUSTMENT,
                title="Bad change",
                description="This will be rejected",
                file_changes=[],
            )

        txn = asyncio.run(run())
        assert txn.status == ExecutorUpgradeStatus.REVIEW_REJECTED
        assert "Bad idea" in txn.error_message

    def test_submit_upgrade_stored_in_transactions(self, tmp_path: Path) -> None:
        executor = _make_executor(tmp_path, auto_git=False)

        async def run() -> UpgradeTransaction:
            mock_reviewer = AsyncMock()
            mock_reviewer.review_upgrade = AsyncMock(
                return_value=type(
                    "R",
                    (),
                    {
                        "verdict": "reject",
                        "feedback": "",
                        "reasoning": "skip",
                    },
                )()
            )
            executor._reviewer = mock_reviewer
            return await executor.submit_upgrade(
                upgrade_type=UpgradeType.POLICY_UPDATE,
                title="Stored",
                description="Check storage",
                file_changes=[],
            )

        txn = asyncio.run(run())
        assert executor.get_transaction(txn.id) is txn


class TestErrorHandlingAutoRollback:
    """When _apply_changes raises, auto-rollback is triggered (with git commit)."""

    def test_apply_failure_sets_failed_status(self, tmp_path: Path) -> None:
        executor = _make_executor(tmp_path, auto_git=False)
        # A verify failure (file not created) should leave status FAILED
        fc = FileChange(path="ghost.py", operation="create", new_content="x")

        txn = UpgradeTransaction(
            id="txn-fail",
            upgrade_type=UpgradeType.CODE_MODIFICATION,
            title="Failing upgrade",
            description="Will fail verify",
            status=ExecutorUpgradeStatus.REVIEW_APPROVED,
            file_changes=[fc],
        )

        # Monkey-patch _apply_changes to raise
        def bad_apply(t: UpgradeTransaction) -> None:
            raise OSError("disk full")

        executor._apply_changes = bad_apply  # type: ignore[method-assign]
        executor._execute_upgrade(txn)
        assert txn.status == ExecutorUpgradeStatus.FAILED
        assert "disk full" in txn.error_message


class TestUpgradeExecutorHelpers:
    """get_transaction, get_all_transactions, rollback, export_history."""

    def test_get_transaction_returns_none_for_unknown_id(self, tmp_path: Path) -> None:
        executor = _make_executor(tmp_path)
        assert executor.get_transaction("no-such-id") is None

    def test_get_all_transactions_empty_initially(self, tmp_path: Path) -> None:
        executor = _make_executor(tmp_path)
        assert executor.get_all_transactions() == []

    def test_rollback_returns_false_for_unknown_transaction(self, tmp_path: Path) -> None:
        executor = _make_executor(tmp_path)
        result = executor.rollback("no-such-id")
        assert result is False

    def test_rollback_returns_false_for_pending_transaction(self, tmp_path: Path) -> None:
        executor = _make_executor(tmp_path)
        txn = _make_transaction()
        executor._transactions[txn.id] = txn
        # PENDING_REVIEW is not rollback-eligible
        result = executor.rollback(txn.id)
        assert result is False

    def test_export_history_writes_json_file(self, tmp_path: Path) -> None:
        executor = _make_executor(tmp_path)
        txn = _make_transaction()
        txn.status = ExecutorUpgradeStatus.COMPLETED
        executor._transactions[txn.id] = txn

        out = tmp_path / "history.json"
        executor.export_history(out)

        data = json.loads(out.read_text())
        assert isinstance(data, list)
        assert data[0]["id"] == txn.id
        assert data[0]["status"] == "completed"


class TestCreateUpgradeFromTask:
    """create_upgrade_from_task factory function."""

    def test_returns_none_for_non_upgrade_task(self, tmp_path: Path) -> None:
        task = Task(
            id="task-001",
            title="Standard task",
            description="Just a task",
            role="backend",
            status=TaskStatus.OPEN,
            task_type=TaskType.STANDARD,
        )
        result = create_upgrade_from_task(task, tmp_path)
        assert result is None

    def test_returns_none_for_upgrade_task_without_details(self, tmp_path: Path) -> None:
        task = Task(
            id="task-002",
            title="Upgrade task with no details",
            description="Missing upgrade_details",
            role="backend",
            status=TaskStatus.OPEN,
            task_type=TaskType.UPGRADE_PROPOSAL,
        )
        result = create_upgrade_from_task(task, tmp_path)
        assert result is None
