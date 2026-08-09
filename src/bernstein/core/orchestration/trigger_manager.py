"""Event-driven trigger manager - evaluates incoming events against user-defined rules.

The TriggerManager is the central coordinator for event-driven agent triggers.
It receives normalized TriggerEvents, matches them against trigger rules loaded
from .sdd/config/triggers.yaml, evaluates conditions (cooldown, dedup, rate
limits), and creates tasks on the task server.
"""

from __future__ import annotations

import contextlib
import datetime
import fnmatch
import hashlib
import json
import logging
import os
import re
import tempfile
import time
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

import yaml

from bernstein.core.defaults import TRIGGER
from bernstein.core.models import (
    TriggerConfig,
    TriggerEvent,
    TriggerFireRecord,
    TriggerTaskTemplate,
)

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.task_store import TaskStore

_FIRE_LOG_FILENAME = "fire_log.jsonl"

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Safety defaults
# ---------------------------------------------------------------------------

_DEFAULT_MAX_TASKS_PER_MINUTE = TRIGGER.max_tasks_per_minute
_DEFAULT_MAX_TASKS_PER_TRIGGER_PER_HOUR = TRIGGER.max_tasks_per_trigger_per_hour
_DEFAULT_EXCLUDE_SENDERS: list[str] = ["bernstein[bot]", "github-actions[bot]"]
_DEFAULT_EXCLUDE_COMMIT_PATTERNS: list[str] = [r"\[bernstein\]"]

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def _parse_task_template(raw: dict[str, Any]) -> TriggerTaskTemplate:
    """Parse the ``task`` section of a trigger config."""
    escalation: dict[int, dict[str, str]] = {}
    for k, v in raw.get("model_escalation", {}).items():
        escalation[int(k)] = dict(v)
    return TriggerTaskTemplate(
        title=raw.get("title", "Triggered task"),
        role=raw.get("role", "backend"),
        priority=int(raw.get("priority", 2)),
        scope=raw.get("scope", "small"),
        task_type=raw.get("task_type", "standard"),
        description_template=raw.get("description_template", ""),
        model=raw.get("model"),
        effort=raw.get("effort"),
        model_escalation=escalation,
    )


def load_trigger_configs(path: Path) -> list[TriggerConfig]:
    """Load trigger rules from a YAML config file.

    Args:
        path: Path to ``triggers.yaml``.

    Returns:
        List of parsed TriggerConfig objects.

    Raises:
        FileNotFoundError: If the config file does not exist.
        ValueError: If the config is malformed.
    """
    if not path.exists():
        raise FileNotFoundError(f"Trigger config not found: {path}")
    try:
        with path.open() as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        raise ValueError(f"Malformed trigger config: {exc}") from exc

    if not isinstance(data, dict) or "triggers" not in data:
        raise ValueError("Trigger config must have a top-level 'triggers' key")

    configs: list[TriggerConfig] = []
    for raw in data["triggers"]:
        if not isinstance(raw, dict) or "name" not in raw or "source" not in raw:
            logger.warning("Skipping malformed trigger entry: %r", raw)
            continue
        # Presence is not type. Both fields are load-bearing: name is joined
        # into the dedup key, where a non-string raises, and source is only
        # ever compared against string literals, so a non-string one loads
        # clean and is silently inert.
        if not isinstance(raw["name"], str) or not raw["name"]:
            logger.warning("Skipping trigger entry with a non-string or empty name: %r", raw["name"])
            continue
        if not isinstance(raw["source"], str) or not raw["source"]:
            logger.warning("Skipping trigger %r: source must be a non-empty string, got %r", raw["name"], raw["source"])
            continue
        # TriggerConfig.schedule is str | None, but YAML hands back whatever was
        # written: `schedule: 30` unquoted arrives as an int and used to be
        # carried in unchecked, making the declared type a fiction and deferring
        # the failure to croniter. Drop non-strings here so the dataclass tells
        # the truth. Syntax is still the evaluator's business - validating it
        # needs croniter, which is optional.
        schedule = raw.get("schedule")
        if raw["source"] == "cron":
            if schedule is not None and not isinstance(schedule, str):
                logger.warning(
                    "Cron trigger %r has a %s schedule, not a string; it will never fire",
                    raw["name"],
                    type(schedule).__name__,
                )
                schedule = None
            elif not schedule:
                # The evaluator skips these on a falsy check every tick, silently.
                logger.warning("Cron trigger %r has no usable schedule and will never fire", raw["name"])
        task_raw = raw.get("task", {})
        configs.append(
            TriggerConfig(
                name=raw["name"],
                source=raw["source"],
                enabled=raw.get("enabled", True),
                filters=dict(raw.get("filters", {})),
                conditions=dict(raw.get("conditions", {})),
                task=_parse_task_template(task_raw),
                schedule=schedule,
            )
        )
    return configs


# ---------------------------------------------------------------------------
# Filter evaluation
# ---------------------------------------------------------------------------


def _glob_match(pattern: str, value: str) -> bool:
    """Match a value against a glob pattern supporting ``**``.

    ``**`` matches zero or more path segments.  This tries both the
    original pattern and a variant with ``**/`` collapsed so that e.g.
    ``src/**/*.py`` matches both ``src/app.py`` and ``src/sub/app.py``.
    """
    if fnmatch.fnmatch(value, pattern):
        return True
    if "**" in pattern:
        # Try with ** collapsed to match zero intermediate directories
        collapsed = pattern.replace("**/", "")
        if fnmatch.fnmatch(value, collapsed):
            return True
        # Also try PurePath.match for deeper paths
        from pathlib import PurePath

        if PurePath(value).match(pattern):
            return True
    return False


def _glob_match_any(patterns: list[str], values: list[str]) -> bool:
    """Return True if any value matches any glob pattern."""
    return any(_glob_match(p, v) for p in patterns for v in values)


def _matches_filter(event: TriggerEvent, trigger: TriggerConfig) -> bool:
    """Evaluate whether a TriggerEvent passes a trigger's filters."""
    filters = trigger.filters

    # Sender exclusion (applies to all sources)
    exclude_senders = filters.get("exclude_senders", []) + _DEFAULT_EXCLUDE_SENDERS
    if event.sender and event.sender in exclude_senders:
        logger.debug("Trigger %s: sender %r excluded", trigger.name, event.sender)
        return False

    _SOURCE_MATCHERS: dict[str, Callable[[TriggerEvent, TriggerConfig], bool]] = {
        "github_push": _matches_github_push,
        "github_workflow_run": _matches_github_workflow_run,
        "slack": _matches_slack,
        "file_watch": _matches_file_watch,
        "webhook": _matches_webhook,
    }
    matcher = _SOURCE_MATCHERS.get(trigger.source)
    if matcher is not None:
        return matcher(event, trigger)
    return True


def _exclude_all_paths(changed_files: frozenset[str], exclude_paths: list[str]) -> bool:
    """Return True when every changed file matches an exclude pattern."""
    from pathlib import PurePath

    remaining = [f for f in changed_files if not any(PurePath(f).match(p) for p in exclude_paths)]
    return not remaining


def _matches_github_push(event: TriggerEvent, trigger: TriggerConfig) -> bool:
    """Filter logic for github_push triggers."""
    filters = trigger.filters
    branches = filters.get("branches", [])
    if branches and event.branch not in branches:
        return False
    paths = filters.get("paths", [])
    if paths and not _glob_match_any(paths, list(event.changed_files)):
        return False
    exclude_paths = filters.get("exclude_paths", [])
    if exclude_paths and event.changed_files and _exclude_all_paths(event.changed_files, exclude_paths):
        return False
    exclude_commit_patterns = filters.get("exclude_commit_patterns", _DEFAULT_EXCLUDE_COMMIT_PATTERNS)
    if event.message:
        for pattern in exclude_commit_patterns:
            if re.search(pattern, event.message):
                logger.debug("Trigger %s: commit message matches exclude pattern %r", trigger.name, pattern)
                return False
    return True


def _matches_github_workflow_run(event: TriggerEvent, trigger: TriggerConfig) -> bool:
    """Filter logic for github_workflow_run triggers."""
    filters = trigger.filters
    conclusion = filters.get("conclusion")
    if conclusion and event.metadata.get("conclusion") != conclusion:
        return False
    workflow_names = filters.get("workflow_names", [])
    if workflow_names and event.metadata.get("workflow_name") not in workflow_names:
        return False
    exclude_workflow_names = filters.get("exclude_workflow_names", [])
    return event.metadata.get("workflow_name") not in exclude_workflow_names


def _matches_slack(event: TriggerEvent, trigger: TriggerConfig) -> bool:
    """Filter logic for slack triggers."""
    filters = trigger.filters
    channels = filters.get("channels", [])
    if channels and event.metadata.get("channel") not in channels:
        return False
    if filters.get("mention_required") and event.message and "@bernstein" not in event.message:
        return False
    msg_pattern = filters.get("message_pattern")
    return not (msg_pattern and event.message and not re.search(msg_pattern, event.message))


def _matches_file_watch(event: TriggerEvent, trigger: TriggerConfig) -> bool:
    """Filter logic for file_watch triggers."""
    filters = trigger.filters
    patterns = filters.get("patterns", [])
    if patterns and not _glob_match_any(patterns, list(event.changed_files)):
        return False
    exclude_patterns = filters.get("exclude_patterns", [])
    if exclude_patterns and event.changed_files and _exclude_all_paths(event.changed_files, exclude_patterns):
        return False
    allowed_events = filters.get("events", [])
    return not (allowed_events and event.metadata.get("event_type") not in allowed_events)


def _matches_webhook(event: TriggerEvent, trigger: TriggerConfig) -> bool:
    """Filter logic for webhook triggers."""
    filters = trigger.filters
    path_filter = filters.get("path")
    if path_filter and event.metadata.get("request_path") != path_filter:
        return False
    method_filter = filters.get("method")
    if method_filter and event.metadata.get("request_method") != method_filter:
        return False
    header_filters: dict[str, str] = filters.get("headers", {})
    request_headers: dict[str, str] = event.metadata.get("request_headers", {})
    return all(request_headers.get(key, "") == expected for key, expected in header_filters.items())


# ---------------------------------------------------------------------------
# Dedup key computation
# ---------------------------------------------------------------------------


def compute_dedup_key(trigger_name: str, event: TriggerEvent) -> str:
    """Compute a deduplication key for a trigger + event pair."""
    # Use SHA-256 of (trigger_name + source + branch/channel/path + sha/timestamp_bucket)
    parts = [trigger_name, event.source]
    if event.branch:
        parts.append(event.branch)
    if event.sha:
        parts.append(event.sha)
    if event.metadata.get("channel"):
        parts.append(event.metadata["channel"])
    if event.metadata.get("request_path"):
        parts.append(event.metadata["request_path"])
    # For sources without a unique key (cron, file_watch), use a 60s bucket
    if event.source in ("cron", "file_watch"):
        bucket = str(int(event.timestamp) // 60)
        parts.append(bucket)
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------


def _infer_role_from_files(files: list[str]) -> str:
    """Infer task role from changed file paths."""
    for f in files:
        if f.startswith(("tests/", "test_")):
            return "qa"
        if f.startswith(("docs/", "README")):
            return "docs"
    return "backend"


def _build_template_variables(trigger: TriggerConfig, event: TriggerEvent) -> dict[str, str]:
    """Build template variable map from trigger config and event data."""
    sha_short = event.sha[:8] if event.sha else ""
    message_preview = (event.message or "")[:60]
    return {
        "branch": event.branch or "",
        "sha": event.sha or "",
        "sha_short": sha_short,
        "sender": event.sender or "",
        "repo": event.repo or "",
        "changed_files": "\n".join(event.changed_files) if event.changed_files else "",
        "changed_count": str(len(event.changed_files)),
        "commit_messages": event.message or "",
        "workflow_name": event.metadata.get("workflow_name", ""),
        "message_text": event.message or "",
        "message_preview": message_preview,
        "channel": event.metadata.get("channel", ""),
        "environment": event.metadata.get("environment", ""),
        "date": time.strftime("%Y-%m-%d"),
        "trigger_name": trigger.name,
    }


def _interpolate_template(text: str, variables: dict[str, str]) -> str:
    """Replace {key} placeholders in text with variable values."""
    result = text
    for key, value in variables.items():
        result = result.replace(f"{{{key}}}", value)
    return result


def render_task_payload(
    trigger: TriggerConfig,
    event: TriggerEvent,
    dedup_key: str,
    retry_count: int = 0,
) -> dict[str, Any]:
    """Render a task creation payload from a trigger template + event.

    Args:
        trigger: The trigger config with task template.
        event: The normalized event.
        dedup_key: Dedup key for audit trail.
        retry_count: Number of prior retries (for model escalation).

    Returns:
        Dict matching ``TaskCreate`` fields.
    """
    template = trigger.task
    variables = _build_template_variables(trigger, event)

    role = template.role
    if role == "auto":
        role = _infer_role_from_files(list(event.changed_files))

    # Model escalation for CI fix triggers
    model = template.model
    effort = template.effort
    if template.model_escalation and retry_count in template.model_escalation:
        esc = template.model_escalation[retry_count]
        model = esc.get("model", model)
        effort = esc.get("effort", effort)

    title = _interpolate_template(template.title, variables)[:120]
    description = _interpolate_template(template.description_template, variables)
    description += f"\n\n<!-- trigger: {trigger.name} source: {event.source} dedup: {dedup_key} -->"

    payload: dict[str, Any] = {
        "title": title,
        "description": description,
        "role": role,
        "priority": template.priority,
        "scope": template.scope,
        "task_type": template.task_type,
    }
    if model:
        payload["model"] = model
    if effort:
        payload["effort"] = effort
    if event.metadata:
        payload["metadata"] = dict(event.metadata)

    return payload


# ---------------------------------------------------------------------------
# TriggerManager
# ---------------------------------------------------------------------------


class TriggerManager:
    """Central coordinator for event-driven triggers.

    Loads trigger rules from ``.sdd/config/triggers.yaml``, evaluates incoming
    events against those rules, enforces conditions (cooldown, dedup, rate
    limits), and creates tasks.
    """

    def __init__(self, sdd_dir: Path, store: TaskStore | None = None) -> None:
        self._sdd_dir = sdd_dir
        self._store = store
        self._config_path = sdd_dir / "config" / "triggers.yaml"
        self._runtime_dir = sdd_dir / "runtime" / "triggers"
        self._runtime_dir.mkdir(parents=True, exist_ok=True)

        self._configs: list[TriggerConfig] = []
        self._config_mtime: float = 0.0

        # In-memory rate limiter: list of fire timestamps in the last 60s
        self._fire_timestamps: list[float] = []
        self._max_tasks_per_minute = _DEFAULT_MAX_TASKS_PER_MINUTE

        # Dedup cache: {dedup_key: expiry_timestamp}
        self._dedup_cache: dict[str, float] = {}

        # Cron state: {trigger_name: last_fire_minute}
        self._cron_state: dict[str, str] = {}
        # Set when _cron_state has changes that are not on disk yet, so a
        # dropped write is retried on the next pass rather than waiting for
        # the next fire (the same-minute dedup skips before the write).
        self._cron_state_dirty = False

        # Load persisted state
        self._load_dedup_cache()
        self._load_cron_state()

        # Try loading config (graceful if missing)
        self._try_reload_config()

    # -- Config loading & hot-reload ----------------------------------------

    def _try_reload_config(self) -> None:
        """Load or hot-reload trigger configs if the file changed."""
        if not self._config_path.exists():
            self._configs = []
            return
        try:
            mtime = self._config_path.stat().st_mtime
            if mtime != self._config_mtime:
                self._configs = load_trigger_configs(self._config_path)
                self._config_mtime = mtime
                # Read global defaults
                with self._config_path.open() as f:
                    data = yaml.safe_load(f)
                defaults = data.get("defaults", {}) if isinstance(data, dict) else {}
                self._max_tasks_per_minute = int(defaults.get("max_tasks_per_minute", _DEFAULT_MAX_TASKS_PER_MINUTE))
                logger.info("Loaded %d trigger configs from %s", len(self._configs), self._config_path)
        except (ValueError, FileNotFoundError) as exc:
            logger.error("Failed to load trigger config: %s", exc)
            self._configs = []

    @property
    def configs(self) -> list[TriggerConfig]:
        """Return current trigger configs, hot-reloading if file changed."""
        self._try_reload_config()
        return self._configs

    @property
    def is_disabled(self) -> bool:
        """Check if the trigger system is disabled (marker file present)."""
        return (self._runtime_dir / "disabled").exists()

    def disable(self, reason: str) -> None:
        """Disable the trigger system by writing a marker file."""
        (self._runtime_dir / "disabled").write_text(reason)
        logger.error("Trigger system disabled: %s", reason)

    def enable(self) -> None:
        """Re-enable the trigger system by removing the marker file."""
        marker = self._runtime_dir / "disabled"
        if marker.exists():
            marker.unlink()
            logger.info("Trigger system re-enabled")

    # -- Dedup cache --------------------------------------------------------

    def _load_dedup_cache(self) -> None:
        path = self._runtime_dir / "dedup_cache.json"
        if path.exists():
            try:
                with path.open() as f:
                    self._dedup_cache = json.load(f)
            except (json.JSONDecodeError, OSError):
                logger.warning("Corrupt dedup cache, treating as empty")
                self._dedup_cache = {}

    def _save_dedup_cache(self) -> None:
        # Prune expired entries
        now = time.time()
        self._dedup_cache = {k: v for k, v in self._dedup_cache.items() if v > now}
        path = self._runtime_dir / "dedup_cache.json"
        with path.open("w") as f:
            json.dump(self._dedup_cache, f)

    def _check_dedup(self, dedup_key: str) -> bool:
        """Return True if the key is a duplicate (should be skipped)."""
        expiry = self._dedup_cache.get(dedup_key)
        return bool(expiry is not None and expiry > time.time())

    def _record_dedup(self, dedup_key: str, ttl_s: int) -> None:
        self._dedup_cache[dedup_key] = time.time() + ttl_s
        self._save_dedup_cache()

    # -- Cron state ---------------------------------------------------------

    def _load_cron_state(self) -> None:
        path = self._runtime_dir / "cron_state.json"
        if path.exists():
            try:
                with path.open() as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError):
                logger.warning("Corrupt cron state, treating as empty")
                self._cron_state = {}
                return
            # Valid JSON of the wrong shape decodes cleanly, so the guard above
            # does not fire and .items()/.get() would raise out of __init__ -
            # taking down every command that builds a TriggerManager. Keep the
            # entries that parse: discarding the whole map re-fires every
            # trigger already recorded for this minute.
            if not isinstance(data, dict):
                logger.warning("Cron state is not an object, treating as empty")
                self._cron_state = {}
                return
            state: dict[str, str] = {}
            for key, value in data.items():
                minute = value.get("last_fire_minute", "") if isinstance(value, dict) else None
                if not isinstance(key, str) or not isinstance(minute, str):
                    logger.warning("Dropping unreadable cron state entry %r", key)
                    continue
                state[key] = minute
            self._cron_state = state

    def _save_cron_state(self) -> None:
        path = self._runtime_dir / "cron_state.json"
        data = {k: {"last_fire_minute": v, "last_fired": time.time()} for k, v in self._cron_state.items()}
        # Write to a scratch sibling and rename over the target. Opening the
        # real file "w" truncates it before the content lands, and
        # _load_cron_state reads a corrupt file as empty state - so an
        # interrupted write did not lose one entry, it replayed every cron
        # trigger on the next start.
        #
        # mkstemp rather than a fixed ".tmp" name: the name is unique per
        # writer, so two managers on one runtime dir cannot interleave inside
        # a single scratch file, and it is created O_EXCL at 0o600, so an
        # existing path cannot capture the write and the mode carries over to
        # cron_state.json through the rename.
        fd, tmp_name = tempfile.mkstemp(dir=self._runtime_dir, prefix="cron_state.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp_name)

    # -- Fire log -----------------------------------------------------------

    def _record_fire(self, record: TriggerFireRecord) -> None:
        """Append a fire record to the fire log."""
        path = self._runtime_dir / _FIRE_LOG_FILENAME
        with path.open("a") as f:
            f.write(json.dumps(asdict(record)) + "\n")

    def _last_fire_time(self, trigger_name: str) -> float | None:
        """Return the most recent fire timestamp for a trigger, or None."""
        path = self._runtime_dir / _FIRE_LOG_FILENAME
        if not path.exists():
            return None
        last: float | None = None
        try:
            for line in path.read_text().strip().split("\n"):
                if not line:
                    continue
                entry = json.loads(line)
                if entry.get("trigger_name") == trigger_name:
                    last = entry.get("fired_at")
        except (json.JSONDecodeError, OSError):
            logger.warning("Error reading fire log")
        return last

    def get_fire_history(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the most recent fire log entries."""
        path = self._runtime_dir / _FIRE_LOG_FILENAME
        if not path.exists():
            return []
        entries: list[dict[str, Any]] = []
        try:
            for line in path.read_text().strip().split("\n"):
                if not line:
                    continue
                entries.append(json.loads(line))
        except (json.JSONDecodeError, OSError):
            return []
        return entries[-limit:]

    # -- Rate limiting ------------------------------------------------------

    def _check_rate_limit(self) -> bool:
        """Return True if global rate limit is exceeded."""
        now = time.time()
        cutoff = now - 60.0
        self._fire_timestamps = [t for t in self._fire_timestamps if t > cutoff]
        return len(self._fire_timestamps) >= self._max_tasks_per_minute

    def _record_rate(self) -> None:
        self._fire_timestamps.append(time.time())

    # -- Condition evaluation -----------------------------------------------

    def _check_conditions(self, trigger: TriggerConfig, event: TriggerEvent) -> str | None:
        """Check trigger conditions. Returns suppression reason or None if all pass."""
        conditions = trigger.conditions

        reason = self._check_cooldown(trigger.name, conditions)
        if reason:
            return reason

        reason = self._check_min_commits(conditions, event)
        if reason:
            return reason

        reason = self._check_max_retries(conditions, trigger)
        if reason:
            return reason

        reason = self._check_skip_if_active(conditions, trigger)
        if reason:
            return reason

        return None

    def _check_cooldown(self, trigger_name: str, conditions: dict[str, Any]) -> str | None:
        """Return suppression reason if cooldown has not elapsed."""
        cooldown_s = conditions.get("cooldown_s", 0)
        if cooldown_s <= 0:
            return None
        last = self._last_fire_time(trigger_name)
        if last is not None and (time.time() - last) < cooldown_s:
            return f"cooldown (last fired {int(time.time() - last)}s ago, cooldown={cooldown_s}s)"
        return None

    @staticmethod
    def _check_min_commits(conditions: dict[str, Any], event: TriggerEvent) -> str | None:
        """Return suppression reason if too few commits in the push."""
        min_commits = conditions.get("min_commits")
        if min_commits is None:
            return None
        commits = event.raw_payload.get("commits", [])
        if len(commits) < min_commits:
            return f"min_commits ({len(commits)} < {min_commits})"
        return None

    def _check_max_retries(self, conditions: dict[str, Any], trigger: TriggerConfig) -> str | None:
        """Return suppression reason if retry limit reached."""
        max_retries = conditions.get("max_retries")
        if max_retries is None or self._store is None:
            return None
        from bernstein.core.models import TaskStatus

        active_statuses = {TaskStatus.OPEN, TaskStatus.CLAIMED, TaskStatus.IN_PROGRESS, TaskStatus.FAILED}
        tasks = self._store.list_tasks()
        title_prefix = trigger.task.title.split("{")[0] if "{" in trigger.task.title else trigger.task.title
        existing = sum(1 for t in tasks if t.title.startswith(title_prefix) and t.status in active_statuses)
        if existing >= max_retries:
            return f"max_retries ({existing}/{max_retries})"
        return None

    def _check_skip_if_active(self, conditions: dict[str, Any], trigger: TriggerConfig) -> str | None:
        """Return suppression reason if a previous task from this trigger is active."""
        if not conditions.get("skip_if_active") or self._store is None:
            return None
        from bernstein.core.models import TaskStatus

        active_statuses = {TaskStatus.OPEN, TaskStatus.CLAIMED, TaskStatus.IN_PROGRESS}
        tasks = self._store.list_tasks()
        active = any(
            t for t in tasks if f"<!-- trigger: {trigger.name}" in t.description and t.status in active_statuses
        )
        if active:
            return "skip_if_active (previous task still active)"
        return None

    # -- Main evaluate pipeline ---------------------------------------------

    def evaluate(
        self,
        event: TriggerEvent,
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """Evaluate an event against all trigger rules.

        This is the main entry point. It runs the full pipeline:
        match → conditions → dedup → render.

        Args:
            event: Normalized trigger event.

        Returns:
            Tuple of (list of TaskCreate payloads, dict of suppressed trigger names → reasons).
        """
        if self.is_disabled:
            return [], {"__system__": "trigger_system_disabled"}

        self._try_reload_config()
        task_payloads: list[dict[str, Any]] = []
        suppressed: dict[str, str] = {}

        # Global rate limit check
        if self._check_rate_limit():
            self.disable(f"Global rate limit exceeded ({self._max_tasks_per_minute} tasks/min)")
            return [], {"__system__": "rate_limit_exceeded"}

        for trigger in self._configs:
            result = self._evaluate_single_trigger(trigger, event, suppressed)
            if result is not None:
                task_payloads.append(result)

        return task_payloads, suppressed

    def _evaluate_single_trigger(
        self,
        trigger: Any,
        event: TriggerEvent,
        suppressed: dict[str, str],
    ) -> dict[str, Any] | None:
        """Evaluate one trigger against an event.

        Returns a task payload dict if the trigger fires, or None if suppressed.
        Records suppression reason in *suppressed* when applicable.
        """
        if not trigger.enabled:
            suppressed[trigger.name] = "disabled"
            return None

        if trigger.source != event.source:
            return None

        if not _matches_filter(event, trigger):
            suppressed[trigger.name] = "no_filter_match"
            return None

        reason = self._check_conditions(trigger, event)
        if reason:
            suppressed[trigger.name] = reason
            logger.info("Trigger %s suppressed by %s", trigger.name, reason)
            return None

        dedup_key = compute_dedup_key(trigger.name, event)
        if self._check_dedup(dedup_key):
            suppressed[trigger.name] = "deduplicated"
            logger.info("Trigger %s deduplicated (key=%s)", trigger.name, dedup_key)
            return None

        retry_count = self._compute_retry_count(trigger)

        try:
            payload = render_task_payload(trigger, event, dedup_key, retry_count)
        except Exception as exc:
            logger.error("Template render error for trigger %s: %s", trigger.name, exc)
            suppressed[trigger.name] = f"template_error: {exc}"
            return None

        cooldown_s = trigger.conditions.get("cooldown_s", 300)
        self._record_dedup(dedup_key, max(cooldown_s, 300))
        self._record_rate()
        logger.info("Trigger %s fired for %s event", trigger.name, event.source)
        return payload

    def _compute_retry_count(self, trigger: Any) -> int:
        """Compute the retry count for model escalation."""
        if not trigger.conditions.get("max_retries") or not self._store:
            return 0
        from bernstein.core.models import TaskStatus

        active_statuses = {TaskStatus.OPEN, TaskStatus.CLAIMED, TaskStatus.IN_PROGRESS, TaskStatus.FAILED}
        tasks = self._store.list_tasks()
        title_prefix = trigger.task.title.split("{")[0] if "{" in trigger.task.title else trigger.task.title
        return sum(1 for t in tasks if t.title.startswith(title_prefix) and t.status in active_statuses)

    def record_fire(self, trigger_name: str, source: str, task_id: str, dedup_key: str, summary: str) -> None:
        """Record a trigger fire after task creation succeeds."""
        record = TriggerFireRecord(
            trigger_name=trigger_name,
            source=source,
            fired_at=time.time(),
            task_id=task_id,
            dedup_key=dedup_key,
            event_summary=summary,
        )
        self._record_fire(record)

    # -- Cron evaluation (called from orchestrator tick) ---------------------

    def evaluate_cron_triggers(self) -> list[TriggerEvent]:
        """Evaluate all cron triggers against the current time.

        Returns a list of synthesized TriggerEvents for cron triggers that
        should fire this minute. Safe to call on every orchestrator tick
        (3s) - uses cron_state to prevent double-firing within the same minute.
        """
        try:
            from croniter import croniter
        except ImportError:
            return []

        self._try_reload_config()
        events: list[TriggerEvent] = []
        now = time.time()
        current_minute = time.strftime("%Y-%m-%dT%H:%M", time.localtime(now))

        for trigger in self._configs:
            if not trigger.enabled or trigger.source != "cron" or not trigger.schedule:
                continue

            # Already fired this minute?
            if self._cron_state.get(trigger.name) == current_minute:
                continue

            try:
                # croniter accepts a Unix timestamp, a datetime, or None - not
                # a struct_time; ``now`` is already a float epoch.
                cron = croniter(trigger.schedule, now)
                prev_fire = cron.get_prev(float)
                # If the previous fire time is within this minute, fire
                prev_minute = time.strftime("%Y-%m-%dT%H:%M", time.localtime(prev_fire))
                # get_prev is strictly-before its anchor, so a tick landing
                # exactly on a fire instant reports the fire *before* it and the
                # schedule reads as not due. match() answers that one instant.
                # It is consulted only after get_prev has already said "not
                # due", so it can add a fire and never suppress one - and it
                # leaves a sub-minute schedule's phase alone, which anchoring
                # the search on the next minute would not.
                if prev_minute != current_minute and not croniter.match(
                    trigger.schedule, datetime.datetime.fromtimestamp(now)
                ):
                    continue
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                logger.error("Invalid cron expression for trigger %s: %s", trigger.name, exc)
                continue

            event = TriggerEvent(
                source="cron",
                timestamp=now,
                raw_payload={"trigger_name": trigger.name, "schedule": trigger.schedule},
                message=f"Cron trigger: {trigger.name}",
                metadata={"cron_name": trigger.name},
            )
            events.append(event)

            # Mark as fired for this minute. The in-memory entry is what
            # suppresses a re-fire on the next tick; the file only carries that
            # across a restart.
            self._cron_state[trigger.name] = current_minute
            self._cron_state_dirty = True

        self._flush_cron_state()
        return events

    def _flush_cron_state(self) -> None:
        """Persist pending cron state, tolerating a failed write.

        Runs once per pass rather than per fire, and unconditionally while
        state is pending, so a dropped write is retried on the next pass
        instead of waiting for some trigger to fire again. A write that never
        lands costs at most a duplicate fire after a restart within the same
        minute; letting the error escape would strand the whole tick, which is
        the failure this module already fixed once.
        """
        if not self._cron_state_dirty:
            return
        try:
            self._save_cron_state()
        except OSError as exc:
            logger.error("Could not persist cron state (will retry next pass): %s", exc)
            return
        self._cron_state_dirty = False

    # -- Summary for CLI ----------------------------------------------------

    def list_triggers(self) -> list[dict[str, Any]]:
        """Return a summary of all configured triggers for CLI display."""
        self._try_reload_config()
        result: list[dict[str, Any]] = []
        for trigger in self._configs:
            last_fire = self._last_fire_time(trigger.name)
            result.append(
                {
                    "name": trigger.name,
                    "source": trigger.source,
                    "enabled": trigger.enabled,
                    "schedule": trigger.schedule,
                    "last_fired": last_fire,
                    "filters": trigger.filters,
                }
            )
        return result
