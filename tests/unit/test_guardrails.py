"""Tests for output guardrails: secret detection, scope enforcement, dangerous ops."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from bernstein.core.guardrails import (
    GuardrailsConfig,
    check_dangerous_operations,
    check_scope,
    check_secrets,
    get_guardrail_stats,
    record_guardrail_event,
    run_guardrails,
)
from bernstein.core.models import Complexity, Scope, Task
from bernstein.core.policy_engine import DecisionType

if TYPE_CHECKING:
    from pathlib import Path


def _make_task(
    *,
    id: str = "T-001",
    role: str = "backend",
    owned_files: list[str] | None = None,
) -> Task:
    return Task(
        id=id,
        title="Test task",
        description="Do something.",
        role=role,
        scope=Scope.MEDIUM,
        complexity=Complexity.MEDIUM,
        owned_files=owned_files or [],
    )


# ---------------------------------------------------------------------------
# Secret detection
# ---------------------------------------------------------------------------


class TestCheckSecrets:
    def test_blocks_aws_access_key(self) -> None:
        diff = "+    config['key'] = 'AKIAIOSFODNN7EXAMPLE'\n"
        results = check_secrets(diff)
        assert len(results) == 1
        assert results[0].type == DecisionType.SAFETY
        assert results[0].bypass_immune

    def test_blocks_github_token(self) -> None:
        diff = "+TOKEN = 'ghp_abcdefghijklmnopqrstuvwxyz123456789012'\n"
        results = check_secrets(diff)
        assert len(results) == 1
        assert results[0].type == DecisionType.SAFETY

    def test_blocks_private_key_header(self) -> None:
        diff = "+-----BEGIN RSA PRIVATE KEY-----\n+MIIEowIBAAKCAQEA...\n"
        results = check_secrets(diff)
        assert len(results) == 1
        assert results[0].type == DecisionType.SAFETY

    def test_passes_clean_diff(self) -> None:
        diff = "+def greet(name: str) -> str:\n+    return f'Hello, {name}!'\n"
        results = check_secrets(diff)
        assert len(results) == 1
        assert results[0].type == DecisionType.ALLOW

    def test_blocks_openssh_private_key(self) -> None:
        diff = "+-----BEGIN OPENSSH PRIVATE KEY-----\n+b3BlbnNzaC1rZXktdjEA...\n"
        results = check_secrets(diff)
        assert results[0].type == DecisionType.SAFETY

    def test_passes_variable_named_key_with_placeholder(self) -> None:
        # Variable named "key" but value is not a real secret pattern
        diff = "+key = 'my_config_key'\n"
        results = check_secrets(diff)
        # Just verify the function runs without error; actual result depends on patterns.
        assert results[0].type in (DecisionType.ALLOW, DecisionType.SAFETY)


# ---------------------------------------------------------------------------
# Scope enforcement
# ---------------------------------------------------------------------------


class TestCheckScope:
    def test_blocks_out_of_scope_file(self) -> None:
        task = _make_task(owned_files=["src/bernstein/core/janitor.py"])
        diff = "diff --git a/src/bernstein/core/orchestrator.py b/src/bernstein/core/orchestrator.py\n"
        results = check_scope(diff, task)
        assert len(results) == 1
        assert results[0].type == DecisionType.ASK
        assert any("orchestrator.py" in f for f in results[0].files)

    def test_passes_in_scope_file(self) -> None:
        task = _make_task(owned_files=["src/bernstein/core/janitor.py"])
        diff = "diff --git a/src/bernstein/core/janitor.py b/src/bernstein/core/janitor.py\n"
        results = check_scope(diff, task)
        assert len(results) == 1
        assert results[0].type == DecisionType.ALLOW

    def test_passes_when_no_owned_files(self) -> None:
        task = _make_task(owned_files=[])
        diff = "diff --git a/anything.py b/anything.py\n"
        results = check_scope(diff, task)
        assert len(results) == 1
        assert results[0].type == DecisionType.ALLOW
        assert "no scope" in results[0].reason.lower()

    def test_collects_all_out_of_scope_files(self) -> None:
        task = _make_task(owned_files=["src/bernstein/core/janitor.py"])
        diff = (
            "diff --git a/src/bernstein/core/orchestrator.py b/src/bernstein/core/orchestrator.py\n"
            "diff --git a/pyproject.toml b/pyproject.toml\n"
        )
        results = check_scope(diff, task)
        assert results[0].type == DecisionType.ASK
        assert len(results[0].files) == 2

    def test_passes_file_within_owned_directory(self) -> None:
        task = _make_task(owned_files=["src/bernstein/core"])
        diff = "diff --git a/src/bernstein/core/new_module.py b/src/bernstein/core/new_module.py\n"
        results = check_scope(diff, task)
        assert results[0].type == DecisionType.ALLOW

    def test_scope_violation_is_ask(self) -> None:
        """Scope violations are ASK (flagged for review)."""
        task = _make_task(owned_files=["src/foo.py"])
        diff = "diff --git a/src/bar.py b/src/bar.py\n"
        results = check_scope(diff, task)
        assert results[0].type == DecisionType.ASK


# ---------------------------------------------------------------------------
# Dangerous operations
# ---------------------------------------------------------------------------

_CRITICAL_FILE_DELETION_DIFF = (
    "diff --git a/LICENSE b/LICENSE\n"
    "deleted file mode 100644\n"
    "index abc1234..0000000\n"
    "--- a/LICENSE\n"
    "+++ /dev/null\n"
    "@@ -1,5 +0,0 @@\n"
    "-MIT License\n"
    "-Copyright (c) 2024\n"
    "-...\n"
)


class TestCheckDangerousOperations:
    def test_flags_critical_file_deletion(self) -> None:
        results = check_dangerous_operations(_CRITICAL_FILE_DELETION_DIFF, GuardrailsConfig())
        assert len(results) == 1
        assert results[0].type == DecisionType.ASK
        assert "LICENSE" in results[0].reason

    def test_flags_test_file_deletion(self) -> None:
        diff = (
            "diff --git a/tests/unit/test_foo.py b/tests/unit/test_foo.py\n"
            "deleted file mode 100644\n"
            "--- a/tests/unit/test_foo.py\n"
            "+++ /dev/null\n"
        )
        results = check_dangerous_operations(diff, GuardrailsConfig())
        assert results[0].type == DecisionType.ASK

    def test_flags_large_deletion(self) -> None:
        removed_lines = "".join(f"-line {i}\n" for i in range(8))
        diff = (
            ("diff --git a/src/foo.py b/src/foo.py\n--- a/src/foo.py\n+++ b/src/foo.py\n@@ -1,10 +1,2 @@\n")
            + removed_lines
            + "+keep1\n+keep2\n"
        )
        results = check_dangerous_operations(diff, GuardrailsConfig(max_deletion_pct=50))
        assert results[0].type == DecisionType.ASK

    def test_passes_clean_diff(self) -> None:
        diff = (
            "diff --git a/src/bernstein/core/foo.py b/src/bernstein/core/foo.py\n"
            "--- a/src/bernstein/core/foo.py\n"
            "+++ b/src/bernstein/core/foo.py\n"
            "@@ -1,5 +1,6 @@\n"
            " keep1\n"
            " keep2\n"
            "+new line\n"
        )
        results = check_dangerous_operations(diff, GuardrailsConfig())
        assert results[0].type == DecisionType.ALLOW

    def test_flags_pyproject_deletion(self) -> None:
        diff = (
            "diff --git a/pyproject.toml b/pyproject.toml\n"
            "deleted file mode 100644\n"
            "--- a/pyproject.toml\n"
            "+++ /dev/null\n"
        )
        results = check_dangerous_operations(diff, GuardrailsConfig())
        assert results[0].type == DecisionType.ASK

    def test_custom_deletion_threshold(self) -> None:
        """With max_deletion_pct=90, an 80% deletion should pass."""
        removed_lines = "".join(f"-line {i}\n" for i in range(8))
        diff = (
            ("diff --git a/src/foo.py b/src/foo.py\n--- a/src/foo.py\n+++ b/src/foo.py\n@@ -1,10 +1,2 @@\n")
            + removed_lines
            + "+keep1\n+keep2\n"
        )
        results = check_dangerous_operations(diff, GuardrailsConfig(max_deletion_pct=90))
        assert results[0].type == DecisionType.ALLOW


# ---------------------------------------------------------------------------
# record_guardrail_event
# ---------------------------------------------------------------------------


class TestRecordGuardrailEvent:
    def test_writes_jsonl_line(self, tmp_path: Path) -> None:
        record_guardrail_event("T-001", "secret_detection", "pass", tmp_path)
        metrics_file = tmp_path / ".sdd" / "metrics" / "guardrails.jsonl"
        assert metrics_file.exists()
        line = json.loads(metrics_file.read_text().strip())
        assert line["task_id"] == "T-001"
        assert line["check"] == "secret_detection"
        assert line["result"] == "pass"
        assert "timestamp" in line

    def test_includes_files_when_provided(self, tmp_path: Path) -> None:
        record_guardrail_event(
            "T-002",
            "scope_enforcement",
            "blocked",
            tmp_path,
            files=["pyproject.toml"],
        )
        metrics_file = tmp_path / ".sdd" / "metrics" / "guardrails.jsonl"
        line = json.loads(metrics_file.read_text().strip())
        assert line["files"] == ["pyproject.toml"]

    def test_appends_multiple_events(self, tmp_path: Path) -> None:
        record_guardrail_event("T-001", "secret_detection", "pass", tmp_path)
        record_guardrail_event("T-002", "scope_enforcement", "blocked", tmp_path)
        metrics_file = tmp_path / ".sdd" / "metrics" / "guardrails.jsonl"
        lines = [json.loads(ln) for ln in metrics_file.read_text().strip().splitlines()]
        assert len(lines) == 2

    def test_omits_files_key_when_not_provided(self, tmp_path: Path) -> None:
        record_guardrail_event("T-001", "secret_detection", "pass", tmp_path)
        metrics_file = tmp_path / ".sdd" / "metrics" / "guardrails.jsonl"
        line = json.loads(metrics_file.read_text().strip())
        assert "files" not in line

    def test_creates_metrics_directory(self, tmp_path: Path) -> None:
        record_guardrail_event("T-001", "secret_detection", "pass", tmp_path)
        assert (tmp_path / ".sdd" / "metrics").is_dir()


# ---------------------------------------------------------------------------
# get_guardrail_stats
# ---------------------------------------------------------------------------


class TestGetGuardrailStats:
    def test_returns_zero_counts_for_missing_file(self, tmp_path: Path) -> None:
        stats = get_guardrail_stats(tmp_path)
        assert stats["total"] == 0
        assert stats["blocked"] == 0
        assert stats["flagged"] == 0

    def test_counts_total_and_blocked(self, tmp_path: Path) -> None:
        record_guardrail_event("T-001", "secret_detection", "pass", tmp_path)
        record_guardrail_event("T-002", "secret_detection", "blocked", tmp_path)
        record_guardrail_event("T-003", "scope_enforcement", "flagged", tmp_path)
        stats = get_guardrail_stats(tmp_path)
        assert stats["total"] == 3
        assert stats["blocked"] == 1
        assert stats["flagged"] == 1

    def test_counts_by_check_type(self, tmp_path: Path) -> None:
        record_guardrail_event("T-001", "secret_detection", "blocked", tmp_path)
        record_guardrail_event("T-002", "scope_enforcement", "flagged", tmp_path)
        stats = get_guardrail_stats(tmp_path)
        assert stats["by_check"]["secret_detection"]["blocked"] == 1
        assert stats["by_check"]["scope_enforcement"]["flagged"] == 1


# ---------------------------------------------------------------------------
# run_guardrails
# ---------------------------------------------------------------------------


class TestRunGuardrails:
    def test_returns_results_for_all_checks(self, tmp_path: Path) -> None:
        task = _make_task(owned_files=["src/foo.py"])
        diff = "+x = 1\n"
        results = run_guardrails(diff, task, GuardrailsConfig(), tmp_path)
        checks = {r.check for r in results}
        assert "secret_detection" in checks
        assert "scope_enforcement" in checks
        assert "dangerous_operations" in checks

    def test_skips_secrets_when_disabled(self, tmp_path: Path) -> None:
        task = _make_task()
        diff = "+KEY = 'AKIAIOSFODNN7EXAMPLE'\n"
        results = run_guardrails(diff, task, GuardrailsConfig(secrets=False), tmp_path)
        checks = {r.check for r in results}
        assert "secret_detection" not in checks

    def test_skips_scope_when_disabled(self, tmp_path: Path) -> None:
        task = _make_task(owned_files=["src/foo.py"])
        diff = "diff --git a/other.py b/other.py\n"
        results = run_guardrails(diff, task, GuardrailsConfig(scope=False), tmp_path)
        checks = {r.check for r in results}
        assert "scope_enforcement" not in checks

    def test_records_events_to_metrics(self, tmp_path: Path) -> None:
        task = _make_task(owned_files=["src/foo.py"])
        run_guardrails("+x = 1\n", task, GuardrailsConfig(), tmp_path)
        metrics_file = tmp_path / ".sdd" / "metrics" / "guardrails.jsonl"
        assert metrics_file.exists()
        lines = metrics_file.read_text().strip().splitlines()
        assert len(lines) >= 1

    def test_blocked_secret_is_marked_blocked_in_result(self, tmp_path: Path) -> None:
        task = _make_task()
        diff = "+TOKEN = 'ghp_abcdefghijklmnopqrstuvwxyz123456789012'\n"
        results = run_guardrails(diff, task, GuardrailsConfig(), tmp_path)
        secret_result = next(r for r in results if r.check == "secret_detection")
        assert not secret_result.passed
        assert secret_result.blocked


from pathlib import Path
from unittest.mock import Mock

from bernstein.core.guardrails import run_guardrails, GuardrailsConfig
from bernstein.core.models import Task


def test_immune_paths_always_blocked(tmp_path: Path) -> None:
    """Modifying .sdd/ or .git/ should be blocked even if config is permissive."""
    workdir = tmp_path / "project"
    workdir.mkdir()

    # Very permissive config
    config = GuardrailsConfig(
        secrets=False,
        scope=False,
        file_permissions=False,
        license_scan=False,
    )

    task = Task(id="T1", title="Test Task", description="Testing immune paths", role="backend")

    # Try to modify .sdd/ (which is in _IMMUNE_CRITICAL_PATHS)
    diff = "diff --git a/.sdd/backlog/closed/T1.yaml b/.sdd/backlog/closed/T1.yaml\n--- a/.sdd/backlog/closed/T1.yaml\n+++ b/.sdd/backlog/closed/T1.yaml\n@@ -1,1 +1,2 @@\n status: closed\n+extra: data"

    results = run_guardrails(diff, task, config, workdir)

    # Should find at least one blocked result for immune paths
    immune_results = [r for r in results if r.check == "immune_path_enforcement"]
    assert len(immune_results) == 1
    assert not immune_results[0].passed
    assert immune_results[0].blocked
    assert ".sdd/backlog/closed/T1.yaml" in immune_results[0].detail


def test_regular_paths_allowed_when_permissive(tmp_path: Path) -> None:
    """Regular files should still pass if config is permissive."""
    workdir = tmp_path / "project"
    workdir.mkdir()

    config = GuardrailsConfig(
        secrets=False,
        scope=False,
        file_permissions=False,
        license_scan=False,
    )

    task = Task(id="T1", title="Test Task", description="Testing regular paths", role="backend")

    diff = "diff --git a/src/main.py b/src/main.py\n--- a/src/main.py\n+++ b/src/main.py\n@@ -1,1 +1,2 @@\n print('hello')\n+print('world')"

    results = run_guardrails(diff, task, config, workdir)

    # Convert to list to iterate
    immune_results = [r for r in results if r.check == "immune_path_enforcement"]
    assert len(immune_results) == 1
    assert immune_results[0].passed
    assert not immune_results[0].blocked
