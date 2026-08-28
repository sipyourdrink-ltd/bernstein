"""Tests for the event-driven trigger manager."""

from __future__ import annotations

import json
import stat
import sys
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any

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

if TYPE_CHECKING:
    from pathlib import Path

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

    @pytest.mark.parametrize(("field", "value"), [("name", 42), ("name", ""), ("source", 7), ("source", None)])
    def test_non_string_name_or_source_is_skipped_at_load(
        self, sdd_dir: Path, caplog: pytest.LogCaptureFixture, field: str, value: Any
    ) -> None:
        """Presence checks are not type checks, and both fields are load-bearing.

        A non-string ``name`` reaches ``compute_dedup_key``, whose ``"|".join``
        raises TypeError. A non-string ``source`` matches no source string, so
        the trigger loads clean and is silently inert.
        """
        entry: dict[str, Any] = {
            "name": "ok",
            "source": "cron",
            "schedule": "* * * * *",
            "task": {"title": "t", "role": "qa"},
        }
        entry[field] = value
        path = sdd_dir / "config" / "triggers.yaml"
        with open(path, "w") as f:
            yaml.dump({"version": 1, "triggers": [entry]}, f)

        with caplog.at_level("WARNING"):
            configs = load_trigger_configs(path)

        assert configs == []
        assert caplog.text

    def test_non_string_cron_schedule_is_rejected_at_load(
        self, sdd_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """``TriggerConfig.schedule`` is ``str | None``; YAML must not smuggle an int in.

        ``schedule: 30`` unquoted decodes as an int and was carried into the
        dataclass unchecked, so the declared type was a fiction and the value
        only failed much later, inside croniter.
        """
        path = sdd_dir / "config" / "triggers.yaml"
        entry = {"name": "int-schedule", "source": "cron", "schedule": 30, "task": {"title": "t", "role": "qa"}}
        with open(path, "w") as f:
            yaml.dump({"version": 1, "triggers": [entry]}, f)

        with caplog.at_level("WARNING"):
            configs = load_trigger_configs(path)

        assert configs[0].schedule is None
        assert "int-schedule" in caplog.text

    @pytest.mark.parametrize("schedule", [None, "", 0])
    def test_cron_trigger_without_usable_schedule_is_reported(
        self, sdd_dir: Path, caplog: pytest.LogCaptureFixture, schedule: Any
    ) -> None:
        """A cron trigger that can never fire is surfaced at load, not silently dropped.

        The evaluator skips these on a falsy check every tick, so without a
        load-time diagnostic an operator gets no signal at all.
        """
        path = sdd_dir / "config" / "triggers.yaml"
        entry: dict[str, Any] = {"name": "ghost", "source": "cron", "task": {"title": "t", "role": "qa"}}
        if schedule is not None:
            entry["schedule"] = schedule
        with open(path, "w") as f:
            yaml.dump({"version": 1, "triggers": [entry]}, f)

        with caplog.at_level("WARNING"):
            configs = load_trigger_configs(path)

        assert len(configs) == 1
        assert "ghost" in caplog.text

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

    @pytest.mark.parametrize("payload", ["null", "[]", '"a string"', "5", "true"])
    def test_corrupt_cron_state_root_does_not_break_construction(self, sdd_dir: Path, payload: str) -> None:
        """Valid JSON of the wrong shape must not abort __init__.

        json.load succeeds, so the (JSONDecodeError, OSError) guard does not
        fire and .items() raises out of the constructor - taking down every
        command that builds a TriggerManager, not just cron evaluation.
        """
        (sdd_dir / "runtime" / "triggers" / "cron_state.json").write_text(payload)

        mgr = TriggerManager(sdd_dir)  # must not raise

        assert mgr._cron_state == {}

    def test_corrupt_cron_state_drops_bad_entries_not_the_whole_file(self, sdd_dir: Path) -> None:
        """One unusable entry must not discard the rest.

        Dropping the whole map re-fires every trigger that was already
        recorded for this minute, so salvage what parses.
        """
        state = {
            "good": {"last_fire_minute": "2023-11-15T00:13"},
            "not-a-mapping": 5,
            "value-wrong-type": {"last_fire_minute": 7},
        }
        (sdd_dir / "runtime" / "triggers" / "cron_state.json").write_text(json.dumps(state))

        mgr = TriggerManager(sdd_dir)

        assert mgr._cron_state == {"good": "2023-11-15T00:13"}

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
# Cron evaluation tests
# ---------------------------------------------------------------------------

# A fixed instant 20s into its minute: the ordinary mid-minute tick. The
# boundary case it used to be chosen to dodge is covered explicitly by
# test_fires_when_the_tick_lands_exactly_on_the_minute.
_FROZEN_NOW = 1_700_000_000.0


class _FrozenTime:
    """Stand-in for the ``time`` module with a pinned ``time()``."""

    def __init__(self, now: float) -> None:
        self._now = now

    def time(self) -> float:
        return self._now

    def __getattr__(self, name: str) -> Any:
        return getattr(time, name)


def _write_triggers(sdd_dir: Path, triggers: list[dict[str, Any]]) -> None:
    path = sdd_dir / "config" / "triggers.yaml"
    with open(path, "w") as f:
        yaml.dump({"version": 1, "triggers": triggers}, f)


def _cron_trigger(name: str, schedule: Any) -> dict[str, Any]:
    return {
        "name": name,
        "source": "cron",
        "enabled": True,
        "schedule": schedule,
        "task": {"title": f"{name} ({{date}})", "role": "manager"},
    }


class TestCronEvaluation:
    """Regression coverage for ``TriggerManager.evaluate_cron_triggers``."""

    @pytest.fixture(autouse=True)
    def _frozen_clock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pytest.importorskip("croniter")
        from bernstein.core.orchestration import trigger_manager as module

        monkeypatch.setattr(module, "time", _FrozenTime(_FROZEN_NOW))

    def test_due_trigger_fires(self, sdd_dir: Path) -> None:
        _write_triggers(sdd_dir, [_cron_trigger("every-minute", "* * * * *")])
        mgr = TriggerManager(sdd_dir)

        events = mgr.evaluate_cron_triggers()

        assert len(events) == 1
        assert events[0].source == "cron"
        assert events[0].metadata["cron_name"] == "every-minute"
        assert events[0].timestamp == _FROZEN_NOW

    def test_fires_when_the_tick_lands_exactly_on_the_minute(
        self, sdd_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A tick at HH:MM:00 must still fire a schedule due that minute.

        ``get_prev`` is strictly-before its anchor, so anchoring on ``now`` at
        an exact boundary returns the *previous* minute and the due trigger is
        skipped. Nothing guarantees a caller's phase, so this is not only a
        test-determinism concern.
        """
        from bernstein.core.orchestration import trigger_manager as module

        boundary = _FROZEN_NOW - (_FROZEN_NOW % 60)
        assert boundary % 60 == 0, "the fixture must sit exactly on a minute"
        monkeypatch.setattr(module, "time", _FrozenTime(boundary))

        _write_triggers(sdd_dir, [_cron_trigger("every-minute", "* * * * *")])
        mgr = TriggerManager(sdd_dir)

        events = mgr.evaluate_cron_triggers()

        assert [e.metadata["cron_name"] for e in events] == ["every-minute"]

    def test_not_due_trigger_does_not_fire_on_the_minute_either(
        self, sdd_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The boundary fix must not turn every schedule into an every-minute one."""
        from bernstein.core.orchestration import trigger_manager as module

        boundary = _FROZEN_NOW - (_FROZEN_NOW % 60)
        monkeypatch.setattr(module, "time", _FrozenTime(boundary))
        off_minute = (time.localtime(boundary).tm_min + 30) % 60
        _write_triggers(sdd_dir, [_cron_trigger("off-minute", f"{off_minute} * * * *")])
        mgr = TriggerManager(sdd_dir)

        assert mgr.evaluate_cron_triggers() == []

    @pytest.mark.parametrize(
        ("offset", "due"), [(0.0, False), (10.0, False), (29.0, False), (30.0, True), (45.0, True)]
    )
    def test_sub_minute_schedule_keeps_its_phase(
        self, sdd_dir: Path, monkeypatch: pytest.MonkeyPatch, offset: float, due: bool
    ) -> None:
        """A 6-field schedule fires at the second it names, not at the minute's start.

        croniter's 6-field form puts seconds last, so ``* * * * * 30`` fires at
        :30 of every minute. Fixing the minute boundary by anchoring the search
        on the *start of the next minute* would report that fire for any tick in
        the minute, moving it up to 59s early. The boundary check is a question
        about ``now`` alone, so it leaves the phase intact.
        """
        from bernstein.core.orchestration import trigger_manager as module

        boundary = _FROZEN_NOW - (_FROZEN_NOW % 60)
        monkeypatch.setattr(module, "time", _FrozenTime(boundary + offset))
        _write_triggers(sdd_dir, [_cron_trigger("half-past", "* * * * * 30")])
        mgr = TriggerManager(sdd_dir)

        assert bool(mgr.evaluate_cron_triggers()) is due

    def test_trigger_not_due_does_not_fire(self, sdd_dir: Path) -> None:
        off_minute = (time.localtime(_FROZEN_NOW).tm_min + 30) % 60
        _write_triggers(sdd_dir, [_cron_trigger("off-minute", f"{off_minute} * * * *")])
        mgr = TriggerManager(sdd_dir)

        assert mgr.evaluate_cron_triggers() == []

    def test_does_not_refire_within_the_same_minute(self, sdd_dir: Path) -> None:
        _write_triggers(sdd_dir, [_cron_trigger("every-minute", "* * * * *")])
        mgr = TriggerManager(sdd_dir)

        assert len(mgr.evaluate_cron_triggers()) == 1
        assert mgr.evaluate_cron_triggers() == []

    def test_state_save_failure_does_not_abort_the_pass(self, sdd_dir: Path) -> None:
        """A failed state write must not strand the triggers evaluated after it."""
        _write_triggers(
            sdd_dir,
            [_cron_trigger("first", "* * * * *"), _cron_trigger("second", "* * * * *")],
        )
        mgr = TriggerManager(sdd_dir)

        def _boom() -> None:
            raise OSError("read-only filesystem")

        mgr._save_cron_state = _boom  # type: ignore[method-assign]

        events = mgr.evaluate_cron_triggers()

        assert [e.metadata["cron_name"] for e in events] == ["first", "second"]

    def test_state_save_failure_still_suppresses_same_minute_refire(self, sdd_dir: Path) -> None:
        """In-memory state survives a failed write, so the fire is not duplicated."""
        _write_triggers(sdd_dir, [_cron_trigger("every-minute", "* * * * *")])
        mgr = TriggerManager(sdd_dir)

        def _boom() -> None:
            raise OSError("read-only filesystem")

        mgr._save_cron_state = _boom  # type: ignore[method-assign]

        assert len(mgr.evaluate_cron_triggers()) == 1
        assert mgr.evaluate_cron_triggers() == []

    def test_failed_state_write_is_retried_on_a_later_pass(self, sdd_dir: Path) -> None:
        """A dropped write is retried even when no trigger fires again.

        The same-minute dedup ``continue`` short-circuits before the write, so
        a pass with nothing new to fire must still flush the pending state or
        the failure is only repaired by an unrelated trigger firing later.
        """
        _write_triggers(sdd_dir, [_cron_trigger("every-minute", "* * * * *")])
        mgr = TriggerManager(sdd_dir)
        real_save = mgr._save_cron_state
        attempts: list[int] = []

        def _flaky() -> None:
            attempts.append(1)
            if len(attempts) == 1:
                raise OSError("read-only filesystem")
            real_save()

        mgr._save_cron_state = _flaky  # type: ignore[method-assign]

        assert len(mgr.evaluate_cron_triggers()) == 1  # fires; the write fails
        assert mgr.evaluate_cron_triggers() == []  # nothing new fires

        assert len(attempts) == 2, "the dropped write was never retried"
        state = json.loads((sdd_dir / "runtime" / "triggers" / "cron_state.json").read_text())
        assert "every-minute" in state

    def test_interrupted_state_write_leaves_the_previous_file_intact(
        self, sdd_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A half-finished write must not degrade to "no state at all".

        _load_cron_state treats a corrupt file as empty, so a truncating write
        that dies mid-serialisation would replay every cron fire on restart.
        """
        _write_triggers(sdd_dir, [_cron_trigger("every-minute", "* * * * *")])
        mgr = TriggerManager(sdd_dir)
        assert len(mgr.evaluate_cron_triggers()) == 1

        state_path = sdd_dir / "runtime" / "triggers" / "cron_state.json"
        good = state_path.read_text()
        assert json.loads(good)

        from bernstein.core.orchestration import trigger_manager as module

        def _dump_then_die(obj: Any, fp: Any, *args: Any, **kwargs: Any) -> None:
            fp.write('{"every-minute": {"last_fire_min')  # truncated on purpose
            raise OSError("no space left on device")

        monkeypatch.setattr(module.json, "dump", _dump_then_die)

        mgr._cron_state["late-arrival"] = "2023-11-15T00:14"
        mgr._cron_state_dirty = True
        mgr._flush_cron_state()

        assert state_path.read_text() == good, "the previous state was clobbered"
        assert mgr._cron_state_dirty is True, "a failed write must stay pending"
        assert self._temp_files(sdd_dir) == [], "a failed write left its scratch file behind"

    @staticmethod
    def _temp_files(sdd_dir: Path) -> list[str]:
        runtime = sdd_dir / "runtime" / "triggers"
        return sorted(p.name for p in runtime.iterdir() if p.name.endswith(".tmp"))

    def test_state_write_is_private_and_leaves_no_scratch_file(self, sdd_dir: Path) -> None:
        """The scratch file is per-writer and private, and never outlives the write.

        A fixed scratch path is shared by every TriggerManager on the box, so
        two of them interleave inside one file before either renames it into
        place.
        """
        _write_triggers(sdd_dir, [_cron_trigger("every-minute", "* * * * *")])
        mgr = TriggerManager(sdd_dir)
        assert len(mgr.evaluate_cron_triggers()) == 1

        state_path = sdd_dir / "runtime" / "triggers" / "cron_state.json"
        assert self._temp_files(sdd_dir) == []
        if sys.platform != "win32":
            # st_mode carries no POSIX permission bits on Windows: a regular
            # file reports 0o666 there (0o444 read-only), whatever mode
            # mkstemp asked for. The scratch-file assertion above holds on
            # both.
            assert stat.S_IMODE(state_path.stat().st_mode) == 0o600

    def test_malformed_schedule_does_not_block_other_triggers(self, sdd_dir: Path) -> None:
        """A schedule croniter rejects is logged and skipped, not raised."""
        _write_triggers(
            sdd_dir,
            [
                _cron_trigger("bad-expression", "not a cron expression"),
                # Dropped to None by load_trigger_configs, so it is skipped on
                # the falsy-schedule guard and never reaches croniter. Kept
                # here because that is the path an operator's YAML takes.
                _cron_trigger("bad-type", 30),
                _cron_trigger("every-minute", "* * * * *"),
            ],
        )
        mgr = TriggerManager(sdd_dir)

        events = mgr.evaluate_cron_triggers()

        assert [e.metadata["cron_name"] for e in events] == ["every-minute"]

    def test_non_string_schedule_reaching_croniter_is_skipped_not_raised(self, sdd_dir: Path) -> None:
        """The evaluator's own guard, exercised on the value croniter chokes on.

        ``load_trigger_configs`` now drops a non-string schedule, so the YAML
        path no longer reaches croniter with one - but the loader is not the
        only way ``_configs`` gets populated, and the evaluator documents that
        it skips a bad entry rather than aborting the pass. croniter answers a
        non-string with ``AttributeError`` and a struct_time with
        ``TypeError``; ``except (ValueError, KeyError)`` caught neither, so a
        single bad entry stranded every trigger behind it.
        """
        _write_triggers(sdd_dir, [_cron_trigger("every-minute", "* * * * *")])
        mgr = TriggerManager(sdd_dir)
        (good,) = mgr._configs
        # mtime is unchanged since __init__ loaded it, so _try_reload_config
        # leaves this in place.
        mgr._configs = [
            replace(good, name="int-schedule", schedule=30),  # type: ignore[arg-type]
            replace(good, name="struct-time-schedule", schedule=time.localtime(_FROZEN_NOW)),  # type: ignore[arg-type]
            good,
        ]

        events = mgr.evaluate_cron_triggers()

        assert [e.metadata["cron_name"] for e in events] == ["every-minute"]


# ---------------------------------------------------------------------------
# Trigger source tests
# ---------------------------------------------------------------------------
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
