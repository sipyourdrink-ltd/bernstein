"""Tests for the event-driven trigger manager."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from bernstein.core.models import TriggerConfig, TriggerEvent, TriggerTaskTemplate
from bernstein.core.trigger_manager import (
    TriggerManager,
    _matches_filter,
    compute_dedup_key,
    load_trigger_configs,
    render_task_payload,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sdd_dir(tmp_path: Path) -> Path:
    """Create a temporary .sdd directory structure."""
    sdd = tmp_path / ".sdd"
    (sdd / "config").mkdir(parents=True)
    (sdd / "runtime" / "triggers").mkdir(parents=True)
    return sdd


@pytest.fixture()
def sample_triggers_yaml() -> dict[str, Any]:
    """Return a sample triggers.yaml content."""
    return {
        "version": 1,
        "defaults": {"max_tasks_per_minute": 10},
        "triggers": [
            {
                "name": "qa-on-push",
                "source": "github_push",
                "enabled": True,
                "filters": {
                    "branches": ["main", "develop"],
                    "paths": ["src/**", "tests/**"],
                    "exclude_paths": [".sdd/**", "docs/**"],
                    "exclude_senders": ["deploy-bot"],
                },
                "conditions": {"min_commits": 1, "cooldown_s": 60},
                "task": {
                    "title": "QA verify push to {branch} ({sha_short})",
                    "role": "qa",
                    "priority": 2,
                    "scope": "small",
                    "task_type": "standard",
                    "description_template": "Commits pushed to {branch}:\n{commit_messages}",
                },
            },
            {
                "name": "ci-fix",
                "source": "github_workflow_run",
                "enabled": True,
                "filters": {
                    "conclusion": "failure",
                    "workflow_names": ["CI", "Tests"],
                },
                "conditions": {"max_retries": 3, "cooldown_s": 30},
                "task": {
                    "title": "[CI-FIX] {workflow_name} failure on {sha_short}",
                    "role": "backend",
                    "priority": 1,
                    "scope": "small",
                    "task_type": "fix",
                    "model_escalation": {
                        0: {"model": "sonnet", "effort": "high"},
                        1: {"model": "sonnet", "effort": "max"},
                        2: {"model": "opus", "effort": "max"},
                    },
                },
            },
            {
                "name": "nightly-evolve",
                "source": "cron",
                "enabled": True,
                "schedule": "0 2 * * *",
                "conditions": {"skip_if_active": True},
                "task": {
                    "title": "Nightly evolution pass ({date})",
                    "role": "manager",
                    "priority": 3,
                    "scope": "medium",
                    "task_type": "research",
                },
            },
            {
                "name": "disabled-trigger",
                "source": "github_push",
                "enabled": False,
                "task": {
                    "title": "Should not fire",
                    "role": "backend",
                },
            },
        ],
    }


@pytest.fixture()
def triggers_yaml_path(sdd_dir: Path, sample_triggers_yaml: dict[str, Any]) -> Path:
    """Write sample triggers.yaml and return its path."""
    path = sdd_dir / "config" / "triggers.yaml"
    with open(path, "w") as f:
        yaml.dump(sample_triggers_yaml, f)
    return path


@pytest.fixture()
def push_event() -> TriggerEvent:
    """Create a sample GitHub push TriggerEvent."""
    return TriggerEvent(
        source="github_push",
        timestamp=time.time(),
        raw_payload={"commits": [{"message": "fix tests"}]},
        repo="acme/widgets",
        branch="main",
        sha="abc12345deadbeef",
        sender="developer",
        changed_files=("src/app.py", "tests/test_app.py"),
        message="fix tests",
        metadata={"commit_count": 1},
    )


@pytest.fixture()
def workflow_event() -> TriggerEvent:
    """Create a sample GitHub workflow_run failure TriggerEvent."""
    return TriggerEvent(
        source="github_workflow_run",
        timestamp=time.time(),
        raw_payload={},
        repo="acme/widgets",
        branch="main",
        sha="def67890abcdef12",
        sender="github-actions",
        message="Workflow 'CI' failure",
        metadata={
            "conclusion": "failure",
            "workflow_name": "CI",
            "run_url": "https://github.com/acme/widgets/actions/runs/123",
        },
    )


# ---------------------------------------------------------------------------
# Config loading tests
# ---------------------------------------------------------------------------


class TestLoadTriggerConfigs:
    def test_load_valid_config(self, triggers_yaml_path: Path) -> None:
        configs = load_trigger_configs(triggers_yaml_path)
        assert len(configs) == 4
        assert configs[0].name == "qa-on-push"
        assert configs[0].source == "github_push"
        assert configs[0].enabled is True
        assert configs[0].task.title == "QA verify push to {branch} ({sha_short})"

    def test_load_missing_config(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_trigger_configs(tmp_path / "nonexistent.yaml")

    def test_load_malformed_yaml(self, sdd_dir: Path) -> None:
        path = sdd_dir / "config" / "triggers.yaml"
        path.write_text("not: [valid yaml\n")
        with pytest.raises(ValueError, match="Malformed"):
            load_trigger_configs(path)

    def test_load_missing_triggers_key(self, sdd_dir: Path) -> None:
        path = sdd_dir / "config" / "triggers.yaml"
        with open(path, "w") as f:
            yaml.dump({"version": 1}, f)
        with pytest.raises(ValueError, match="triggers"):
            load_trigger_configs(path)

    def test_model_escalation_parsed(self, triggers_yaml_path: Path) -> None:
        configs = load_trigger_configs(triggers_yaml_path)
        ci_fix = next(c for c in configs if c.name == "ci-fix")
        assert ci_fix.task.model_escalation[0] == {"model": "sonnet", "effort": "high"}
        assert ci_fix.task.model_escalation[2] == {"model": "opus", "effort": "max"}


# ---------------------------------------------------------------------------
# Filter evaluation tests
# ---------------------------------------------------------------------------


class TestMatchesFilter:
    def test_push_matching_branch_and_path(self, push_event: TriggerEvent) -> None:
        trigger = TriggerConfig(
            name="test",
            source="github_push",
            filters={"branches": ["main"], "paths": ["src/**"]},
        )
        assert _matches_filter(push_event, trigger) is True

    def test_push_wrong_branch(self, push_event: TriggerEvent) -> None:
        trigger = TriggerConfig(
            name="test",
            source="github_push",
            filters={"branches": ["develop"]},
        )
        assert _matches_filter(push_event, trigger) is False

    def test_push_excluded_path(self) -> None:
        event = TriggerEvent(
            source="github_push",
            timestamp=time.time(),
            raw_payload={},
            branch="main",
            sender="developer",
            changed_files=("docs/README.md",),
        )
        trigger = TriggerConfig(
            name="test",
            source="github_push",
            filters={"exclude_paths": ["docs/**"]},
        )
        assert _matches_filter(event, trigger) is False

    def test_push_excluded_sender(self) -> None:
        event = TriggerEvent(
            source="github_push",
            timestamp=time.time(),
            raw_payload={},
            branch="main",
            sender="bernstein[bot]",
            changed_files=("src/app.py",),
        )
        trigger = TriggerConfig(name="test", source="github_push")
        assert _matches_filter(event, trigger) is False

    def test_push_commit_pattern_exclusion(self) -> None:
        event = TriggerEvent(
            source="github_push",
            timestamp=time.time(),
            raw_payload={},
            branch="main",
            sender="developer",
            changed_files=("src/app.py",),
            message="[bernstein] auto-fix linting",
        )
        trigger = TriggerConfig(name="test", source="github_push")
        assert _matches_filter(event, trigger) is False

    def test_workflow_run_matching_conclusion(self, workflow_event: TriggerEvent) -> None:
        trigger = TriggerConfig(
            name="test",
            source="github_workflow_run",
            filters={"conclusion": "failure", "workflow_names": ["CI"]},
        )
        assert _matches_filter(workflow_event, trigger) is True

    def test_workflow_run_wrong_conclusion(self, workflow_event: TriggerEvent) -> None:
        trigger = TriggerConfig(
            name="test",
            source="github_workflow_run",
            filters={"conclusion": "success"},
        )
        assert _matches_filter(workflow_event, trigger) is False

    def test_workflow_run_excluded_workflow_name(self, workflow_event: TriggerEvent) -> None:
        trigger = TriggerConfig(
            name="test",
            source="github_workflow_run",
            filters={"exclude_workflow_names": ["CI"]},
        )
        assert _matches_filter(workflow_event, trigger) is False

    def test_slack_channel_filter(self) -> None:
        event = TriggerEvent(
            source="slack",
            timestamp=time.time(),
            raw_payload={},
            sender="user123",
            message="@bernstein fix the login bug",
            metadata={"channel": "#bernstein-tasks"},
        )
        trigger = TriggerConfig(
            name="test",
            source="slack",
            filters={"channels": ["#bernstein-tasks"], "mention_required": True},
        )
        assert _matches_filter(event, trigger) is True

    def test_slack_wrong_channel(self) -> None:
        event = TriggerEvent(
            source="slack",
            timestamp=time.time(),
            raw_payload={},
            message="@bernstein fix it",
            metadata={"channel": "#random"},
        )
        trigger = TriggerConfig(
            name="test",
            source="slack",
            filters={"channels": ["#bernstein-tasks"]},
        )
        assert _matches_filter(event, trigger) is False

    def test_slack_no_mention(self) -> None:
        event = TriggerEvent(
            source="slack",
            timestamp=time.time(),
            raw_payload={},
            message="fix the login bug",
            metadata={"channel": "#bernstein-tasks"},
        )
        trigger = TriggerConfig(
            name="test",
            source="slack",
            filters={"mention_required": True},
        )
        assert _matches_filter(event, trigger) is False

    def test_file_watch_matching_pattern(self) -> None:
        event = TriggerEvent(
            source="file_watch",
            timestamp=time.time(),
            raw_payload={},
            changed_files=("src/app.py",),
            metadata={"event_type": "modified"},
        )
        trigger = TriggerConfig(
            name="test",
            source="file_watch",
            filters={"patterns": ["src/**/*.py"], "events": ["modified"]},
        )
        assert _matches_filter(event, trigger) is True

    def test_file_watch_excluded_pattern(self) -> None:
        event = TriggerEvent(
            source="file_watch",
            timestamp=time.time(),
            raw_payload={},
            changed_files=("src/__pycache__/app.cpython-312.pyc",),
            metadata={"event_type": "modified"},
        )
        trigger = TriggerConfig(
            name="test",
            source="file_watch",
            filters={"exclude_patterns": ["**/__pycache__/**"]},
        )
        assert _matches_filter(event, trigger) is False

    def test_webhook_path_match(self) -> None:
        event = TriggerEvent(
            source="webhook",
            timestamp=time.time(),
            raw_payload={},
            metadata={
                "request_path": "/webhooks/trigger/deploy",
                "request_method": "POST",
                "request_headers": {"X-Trigger-Secret": "mysecret"},
            },
        )
        trigger = TriggerConfig(
            name="test",
            source="webhook",
            filters={
                "path": "/webhooks/trigger/deploy",
                "method": "POST",
                "headers": {"X-Trigger-Secret": "mysecret"},
            },
        )
        assert _matches_filter(event, trigger) is True

    def test_webhook_wrong_secret(self) -> None:
        event = TriggerEvent(
            source="webhook",
            timestamp=time.time(),
            raw_payload={},
            metadata={
                "request_path": "/webhooks/trigger/deploy",
                "request_method": "POST",
                "request_headers": {"X-Trigger-Secret": "wrong"},
            },
        )
        trigger = TriggerConfig(
            name="test",
            source="webhook",
            filters={"headers": {"X-Trigger-Secret": "mysecret"}},
        )
        assert _matches_filter(event, trigger) is False


# ---------------------------------------------------------------------------
# Dedup key tests
# ---------------------------------------------------------------------------


class TestDedupKey:
    def test_same_event_same_key(self, push_event: TriggerEvent) -> None:
        key1 = compute_dedup_key("trigger-a", push_event)
        key2 = compute_dedup_key("trigger-a", push_event)
        assert key1 == key2

    def test_different_trigger_different_key(self, push_event: TriggerEvent) -> None:
        key1 = compute_dedup_key("trigger-a", push_event)
        key2 = compute_dedup_key("trigger-b", push_event)
        assert key1 != key2

    def test_different_sha_different_key(self) -> None:
        event1 = TriggerEvent(source="github_push", timestamp=time.time(), raw_payload={}, sha="abc123")
        event2 = TriggerEvent(source="github_push", timestamp=time.time(), raw_payload={}, sha="def456")
        key1 = compute_dedup_key("trigger-a", event1)
        key2 = compute_dedup_key("trigger-a", event2)
        assert key1 != key2

    def test_cron_uses_minute_bucket(self) -> None:
        # Use a timestamp at the start of a minute to ensure +30s stays in same bucket
        now = float(int(time.time()) // 60 * 60)
        event1 = TriggerEvent(source="cron", timestamp=now, raw_payload={})
        event2 = TriggerEvent(source="cron", timestamp=now + 30, raw_payload={})
        key1 = compute_dedup_key("cron-trigger", event1)
        key2 = compute_dedup_key("cron-trigger", event2)
        # Same minute → same key
        assert key1 == key2


# ---------------------------------------------------------------------------
# Template rendering tests
# ---------------------------------------------------------------------------


class TestRenderTaskPayload:
    def test_basic_rendering(self, push_event: TriggerEvent) -> None:
        trigger = TriggerConfig(
            name="qa-on-push",
            source="github_push",
            task=TriggerTaskTemplate(
                title="QA verify push to {branch} ({sha_short})",
                role="qa",
                priority=2,
                scope="small",
                description_template="Branch: {branch}\nSHA: {sha}",
            ),
        )
        payload = render_task_payload(trigger, push_event, "dedup123")
        assert payload["title"] == "QA verify push to main (abc12345)"
        assert payload["role"] == "qa"
        assert "Branch: main" in payload["description"]
        assert "<!-- trigger: qa-on-push" in payload["description"]

    def test_auto_role_inference_tests(self) -> None:
        event = TriggerEvent(
            source="github_push",
            timestamp=time.time(),
            raw_payload={},
            changed_files=("tests/test_auth.py",),
        )
        trigger = TriggerConfig(
            name="test",
            source="github_push",
            task=TriggerTaskTemplate(title="Test", role="auto"),
        )
        payload = render_task_payload(trigger, event, "key")
        assert payload["role"] == "qa"

    def test_auto_role_inference_docs(self) -> None:
        event = TriggerEvent(
            source="github_push",
            timestamp=time.time(),
            raw_payload={},
            changed_files=("docs/API.md",),
        )
        trigger = TriggerConfig(
            name="test",
            source="github_push",
            task=TriggerTaskTemplate(title="Test", role="auto"),
        )
        payload = render_task_payload(trigger, event, "key")
        assert payload["role"] == "docs"

    def test_model_escalation(self) -> None:
        event = TriggerEvent(
            source="github_workflow_run",
            timestamp=time.time(),
            raw_payload={},
        )
        trigger = TriggerConfig(
            name="ci-fix",
            source="github_workflow_run",
            task=TriggerTaskTemplate(
                title="Fix CI",
                model_escalation={
                    0: {"model": "sonnet", "effort": "high"},
                    2: {"model": "opus", "effort": "max"},
                },
            ),
        )
        p0 = render_task_payload(trigger, event, "key", retry_count=0)
        assert p0["model"] == "sonnet"
        assert p0["effort"] == "high"

        p2 = render_task_payload(trigger, event, "key", retry_count=2)
        assert p2["model"] == "opus"
        assert p2["effort"] == "max"


# ---------------------------------------------------------------------------
# TriggerManager integration tests
# ---------------------------------------------------------------------------


class TestTriggerManager:
    def test_init_graceful_no_config(self, sdd_dir: Path) -> None:
        mgr = TriggerManager(sdd_dir)
        assert mgr.configs == []
        assert not mgr.is_disabled

    def test_load_config(self, sdd_dir: Path, triggers_yaml_path: Path) -> None:
        mgr = TriggerManager(sdd_dir)
        assert len(mgr.configs) == 4
        assert mgr.configs[0].name == "qa-on-push"

    def test_evaluate_push_happy_path(self, sdd_dir: Path, triggers_yaml_path: Path, push_event: TriggerEvent) -> None:
        mgr = TriggerManager(sdd_dir)
        payloads, suppressed = mgr.evaluate(push_event)
        assert len(payloads) == 1
        assert "QA verify push to main" in payloads[0]["title"]
        assert suppressed.get("disabled-trigger") == "disabled"

    def test_evaluate_filtered_by_branch(self, sdd_dir: Path, triggers_yaml_path: Path) -> None:
        event = TriggerEvent(
            source="github_push",
            timestamp=time.time(),
            raw_payload={"commits": [{"message": "fix"}]},
            branch="feature/x",
            sender="developer",
            changed_files=("src/app.py",),
            message="fix",
        )
        mgr = TriggerManager(sdd_dir)
        payloads, suppressed = mgr.evaluate(event)
        assert len(payloads) == 0
        assert "qa-on-push" in suppressed

    def test_evaluate_sender_exclusion(self, sdd_dir: Path, triggers_yaml_path: Path) -> None:
        event = TriggerEvent(
            source="github_push",
            timestamp=time.time(),
            raw_payload={"commits": [{"message": "auto"}]},
            branch="main",
            sender="bernstein[bot]",
            changed_files=("src/app.py",),
            message="auto",
        )
        mgr = TriggerManager(sdd_dir)
        payloads, _ = mgr.evaluate(event)
        assert len(payloads) == 0

    def test_evaluate_cooldown_suppression(
        self, sdd_dir: Path, triggers_yaml_path: Path, push_event: TriggerEvent
    ) -> None:
        mgr = TriggerManager(sdd_dir)
        # First evaluation fires
        payloads1, _ = mgr.evaluate(push_event)
        assert len(payloads1) == 1
        # Record a fire
        mgr.record_fire("qa-on-push", "github_push", "task1", "dedup1", "push to main")

        # Second evaluation within cooldown should be suppressed
        payloads2, suppressed2 = mgr.evaluate(push_event)
        assert len(payloads2) == 0
        assert "cooldown" in suppressed2.get("qa-on-push", "")

    def test_dedup_prevents_duplicate(self, sdd_dir: Path, triggers_yaml_path: Path, push_event: TriggerEvent) -> None:
        mgr = TriggerManager(sdd_dir)
        # First evaluation fires and records dedup
        payloads1, _ = mgr.evaluate(push_event)
        assert len(payloads1) == 1

        # Same event again should be deduplicated
        payloads2, suppressed2 = mgr.evaluate(push_event)
        assert len(payloads2) == 0
        assert suppressed2.get("qa-on-push") == "deduplicated"

    def test_disabled_trigger_skipped(self, sdd_dir: Path, triggers_yaml_path: Path) -> None:
        event = TriggerEvent(
            source="github_push",
            timestamp=time.time(),
            raw_payload={"commits": [{"message": "test"}]},
            branch="main",
            sender="developer",
            changed_files=("src/app.py",),
            message="test",
        )
        mgr = TriggerManager(sdd_dir)
        _, suppressed = mgr.evaluate(event)
        assert suppressed.get("disabled-trigger") == "disabled"

    def test_disable_enable_system(self, sdd_dir: Path, triggers_yaml_path: Path, push_event: TriggerEvent) -> None:
        mgr = TriggerManager(sdd_dir)
        mgr.disable("test reason")
        assert mgr.is_disabled
        payloads, suppressed = mgr.evaluate(push_event)
        assert len(payloads) == 0
        assert "__system__" in suppressed

        mgr.enable()
        assert not mgr.is_disabled

    def test_rate_limit_disables_system(self, sdd_dir: Path, triggers_yaml_path: Path) -> None:
        mgr = TriggerManager(sdd_dir)
        # Simulate hitting rate limit
        mgr._fire_timestamps = [time.time()] * 10  # max_tasks_per_minute = 10

        event = TriggerEvent(
            source="github_push",
            timestamp=time.time(),
            raw_payload={"commits": [{"message": "test"}]},
            branch="main",
            sender="developer",
            changed_files=("src/app.py",),
            message="test",
        )
        payloads, suppressed = mgr.evaluate(event)
        assert len(payloads) == 0
        assert "__system__" in suppressed
        assert mgr.is_disabled

    def test_fire_history(self, sdd_dir: Path, triggers_yaml_path: Path) -> None:
        mgr = TriggerManager(sdd_dir)
        mgr.record_fire("qa-on-push", "github_push", "task1", "dedup1", "push to main")
        mgr.record_fire("ci-fix", "github_workflow_run", "task2", "dedup2", "CI failure")

        history = mgr.get_fire_history()
        assert len(history) == 2
        assert history[0]["trigger_name"] == "qa-on-push"
        assert history[1]["trigger_name"] == "ci-fix"

    def test_list_triggers(self, sdd_dir: Path, triggers_yaml_path: Path) -> None:
        mgr = TriggerManager(sdd_dir)
        triggers = mgr.list_triggers()
        assert len(triggers) == 4
        assert triggers[0]["name"] == "qa-on-push"
        assert triggers[0]["source"] == "github_push"
        assert triggers[0]["enabled"] is True

    def test_hot_reload(self, sdd_dir: Path, triggers_yaml_path: Path) -> None:
        mgr = TriggerManager(sdd_dir)
        assert len(mgr.configs) == 4

        # Add a new trigger to the config
        with open(triggers_yaml_path) as f:
            data = yaml.safe_load(f)
        data["triggers"].append(
            {
                "name": "new-trigger",
                "source": "webhook",
                "task": {"title": "New trigger", "role": "backend"},
            }
        )
        with open(triggers_yaml_path, "w") as f:
            yaml.dump(data, f)

        # Force reload by clearing mtime
        mgr._config_mtime = 0.0
        assert len(mgr.configs) == 5

    def test_workflow_run_evaluation(
        self, sdd_dir: Path, triggers_yaml_path: Path, workflow_event: TriggerEvent
    ) -> None:
        mgr = TriggerManager(sdd_dir)
        payloads, _ = mgr.evaluate(workflow_event)
        assert len(payloads) == 1
        assert "[CI-FIX]" in payloads[0]["title"]
        assert payloads[0]["task_type"] == "fix"

    def test_multiple_triggers_match(self, sdd_dir: Path) -> None:
        """One push event matching 2 different triggers creates 2 tasks."""
        config = {
            "version": 1,
            "triggers": [
                {
                    "name": "qa-check",
                    "source": "github_push",
                    "filters": {"branches": ["main"]},
                    "task": {"title": "QA: {branch}", "role": "qa"},
                },
                {
                    "name": "lint-check",
                    "source": "github_push",
                    "filters": {"branches": ["main"]},
                    "task": {"title": "Lint: {branch}", "role": "backend"},
                },
            ],
        }
        path = sdd_dir / "config" / "triggers.yaml"
        with open(path, "w") as f:
            yaml.dump(config, f)

        mgr = TriggerManager(sdd_dir)
        event = TriggerEvent(
            source="github_push",
            timestamp=time.time(),
            raw_payload={"commits": [{"message": "fix"}]},
            branch="main",
            sender="developer",
            changed_files=("src/app.py",),
        )
        payloads, _ = mgr.evaluate(event)
        assert len(payloads) == 2
        titles = {p["title"] for p in payloads}
        assert "QA: main" in titles
        assert "Lint: main" in titles


# ---------------------------------------------------------------------------
# Trigger source tests
# ---------------------------------------------------------------------------


class TestGitHubSources:
    def test_normalize_push(self) -> None:
        from bernstein.core.trigger_sources.github import normalize_push

        payload = {
            "ref": "refs/heads/main",
            "head_commit": {"id": "abc123"},
            "commits": [
                {"message": "fix bug", "added": ["new.py"], "modified": ["app.py"], "removed": []},
            ],
        }
        event = normalize_push(payload, "developer", "acme/widgets")
        assert event.source == "github_push"
        assert event.branch == "main"
        assert event.sha == "abc123"
        assert event.sender == "developer"
        assert "new.py" in event.changed_files
        assert "app.py" in event.changed_files

    def test_normalize_workflow_run(self) -> None:
        from bernstein.core.trigger_sources.github import normalize_workflow_run

        payload = {
            "workflow_run": {
                "name": "CI",
                "conclusion": "failure",
                "head_branch": "main",
                "head_sha": "def456",
                "html_url": "https://github.com/acme/widgets/actions/runs/123",
            }
        }
        event = normalize_workflow_run(payload, "github-actions", "acme/widgets")
        assert event.source == "github_workflow_run"
        assert event.metadata["conclusion"] == "failure"
        assert event.metadata["workflow_name"] == "CI"

    def test_normalize_issues(self) -> None:
        from bernstein.core.trigger_sources.github import normalize_issues

        payload = {
            "issue": {
                "number": 42,
                "title": "Fix parser",
                "body": "The parser crashes.",
            }
        }
        event = normalize_issues(payload, "octocat", "acme/widgets", "opened")
        assert event.source == "github_issues"
        assert event.metadata["issue_number"] == 42
        assert event.metadata["action"] == "opened"


class TestSlackSource:
    def test_verify_slack_signature(self) -> None:
        from bernstein.core.trigger_sources.slack import verify_slack_signature

        body = b'{"type":"event_callback"}'
        secret = "test-secret"
        ts = str(int(time.time()))

        import hashlib
        import hmac as _hmac

        sig_basestring = f"v0:{ts}:{body.decode('utf-8')}"
        computed = "v0=" + _hmac.new(secret.encode(), sig_basestring.encode(), hashlib.sha256).hexdigest()

        assert verify_slack_signature(body, ts, computed, secret) is True
        assert verify_slack_signature(body, ts, "v0=bad", secret) is False

    def test_verify_rejects_old_timestamp(self) -> None:
        from bernstein.core.trigger_sources.slack import verify_slack_signature

        old_ts = str(int(time.time()) - 600)  # 10 minutes ago
        assert verify_slack_signature(b"body", old_ts, "v0=sig", "secret") is False

    def test_normalize_slack_message(self) -> None:
        from bernstein.core.trigger_sources.slack import normalize_slack_message

        payload = {
            "type": "event_callback",
            "team_id": "T12345",
            "event": {
                "type": "message",
                "channel": "C12345",
                "user": "U12345",
                "text": "@bernstein fix the login bug",
                "ts": "1711670400.000100",
            },
        }
        event = normalize_slack_message(payload)
        assert event.source == "slack"
        assert event.sender == "U12345"
        assert event.message == "@bernstein fix the login bug"
        assert event.metadata["channel"] == "C12345"


class TestWebhookSource:
    def test_normalize_webhook(self) -> None:
        from bernstein.core.trigger_sources.webhook import normalize_webhook

        event = normalize_webhook(
            path="/webhooks/trigger/deploy",
            method="POST",
            headers={"X-Trigger-Secret": "mysecret"},
            payload={"environment": "staging", "version": "1.2.3"},
        )
        assert event.source == "webhook"
        assert event.metadata["request_path"] == "/webhooks/trigger/deploy"
        assert event.metadata["environment"] == "staging"

    def test_interpolate_env_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from bernstein.core.trigger_sources.webhook import interpolate_env_vars

        monkeypatch.setenv("MY_SECRET", "abc123")
        assert interpolate_env_vars("{MY_SECRET}") == "abc123"
        assert interpolate_env_vars("no-vars") == "no-vars"


class TestFileWatchSource:
    def test_drain_empty(self) -> None:
        from bernstein.core.trigger_sources.file_watch import FileWatchSource

        source = FileWatchSource()
        assert source.drain_events() == []

    def test_drain_coalesces_events(self) -> None:
        from bernstein.core.trigger_sources.file_watch import FileWatchSource

        source = FileWatchSource()
        # Manually push events into the queue
        source._on_fs_event("/tmp/a.py", "modified")
        source._on_fs_event("/tmp/b.py", "created")
        source._on_fs_event("/tmp/a.py", "modified")  # duplicate

        events = source.drain_events()
        assert len(events) == 1
        # Coalesced event should have deduplicated files
        assert "/tmp/a.py" in events[0].changed_files
        assert "/tmp/b.py" in events[0].changed_files
