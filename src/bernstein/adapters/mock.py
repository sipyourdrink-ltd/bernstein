"""Mock CLI adapter for zero-API-key demos and testing."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from typing import TYPE_CHECKING, Any

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult
from bernstein.adapters.env_isolation import build_filtered_env

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ModelConfig


class MockAgentAdapter(CLIAdapter):
    """Simulates an agent without making real API calls.

    Used for demos and testing. Spawns a subprocess that applies
    pre-scripted bug fixes to the demo project and exits successfully.
    """

    # Default model when no operator-pinned model reaches this adapter. Read by
    # the spawner to substitute Claude cascade tier names (opus/sonnet/haiku)
    # for this non-Claude adapter. Without it the spawn-time model gate refuses
    # to spawn a Claude tier on the mock adapter, so ``bernstein demo`` fails
    # every task (issue #2799). The mock ignores the model at runtime; the name
    # only has to be a non-Claude-tier placeholder the gate can coerce to.
    default_model = "mock"

    def spawn(
        self,
        *,
        prompt: str,
        workdir: Path,
        model_config: ModelConfig,
        session_id: str,
        mcp_config: dict[str, Any] | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        task_scope: str = "medium",
        budget_multiplier: float = 1.0,
        system_addendum: str = "",
        multimodal_context: Any | None = None,
        task_id: str = "",
        task_title: str = "",
    ) -> SpawnResult:
        """Spawn a mock agent subprocess that applies demo changes.

        Args:
            prompt: Agent task description (unused for identity).
            workdir: Project root directory.
            model_config: Model configuration (unused for mock).
            session_id: Unique session identifier.
            mcp_config: MCP configuration (unused for mock).
            task_id: The real task identifier, threaded through to the adapter
                to avoid prompt-based identity matching (issue #3629).
            task_title: The unique task title, used to determine which fix to apply.

        Returns:
            SpawnResult with mock process PID and log path.
        """
        self.refuse_multimodal_if_needed(multimodal_context)
        log_path = workdir / ".sdd" / "runtime" / f"agent-{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        script_content = self._build_mock_script()
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".py",
            delete=False,
            dir=workdir / ".sdd" / "runtime",
            encoding="utf-8",
        ) as tmp:
            tmp.write(script_content)
            tmp.flush()
            script_path = tmp.name

        task_info = json.dumps(
            {
                "workdir": str(workdir),
                "log_path": str(log_path),
                "task_id": task_id,
                "task_title": task_title,
            }
        )

        cmd = [
            sys.executable,
            script_path,
            task_info,
        ]

        env = build_filtered_env(
            [
                "BERNSTEIN_MOCK_IDLE",
                "BERNSTEIN_MOCK_IDLE_MIN_S",
                "BERNSTEIN_MOCK_IDLE_MAX_S",
                "BERNSTEIN_MOCK_FAIL_RATE",
            ]
        )
        proc = subprocess.Popen(
            cmd,
            cwd=str(workdir),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        result = SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def name(self) -> str:
        """Return adapter name."""
        return "mock"

    @staticmethod
    def agent_script_source() -> str:
        """The mock agent's program text, for callers that own their own spawn.

        :meth:`spawn` builds the environment and starts the process itself,
        which is right for the orchestrator and wrong for a caller whose whole
        job is owning those two things - the volunteer runner has to start the
        process under its own wall-clock cap with an environment derived from a
        sandbox profile, so it needs the program rather than a running process.

        Exposed rather than duplicated so a zero-key volunteer run exercises
        the same agent the rest of the suite does instead of a lookalike that
        can drift away from it.

        Returns:
            Python source for the mock agent worker.
        """
        return MockAgentAdapter._build_mock_script()

    @staticmethod
    def _build_mock_script() -> str:
        """Build a Python script that simulates agent bug-fix work.

        Returns:
            Python script source code (written to a temp file and executed).
        """
        return r'''#!/usr/bin/env python3
"""Mock agent worker that simulates bug-fix task completion."""
import json
import sys
import time
from pathlib import Path


def write_log(path: Path, message: str) -> None:
    """Append message to log file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(f"{time.time()} {message}\n")


def record_modified(log_path: Path, rel_path: str) -> None:
    """Write the completion-evidence line the orchestrator parses."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a") as f:
        f.write(f"Modified: {rel_path}\n")


def commit_fix(workdir: Path, log_path: Path, message: str, rel_path: str) -> None:
    """Commit exactly the fixed file on the worktree branch."""
    import subprocess

    git = [
        "git",
        "-c",
        "user.name=bernstein-mock-agent",
        "-c",
        "user.email=mock-agent@bernstein.invalid",
    ]
    try:
        subprocess.run([*git, "add", "--", rel_path], cwd=workdir, check=True, capture_output=True, timeout=30)
        subprocess.run(
            [*git, "commit", "-m", message, "--", rel_path],
            cwd=workdir,
            check=True,
            capture_output=True,
            timeout=30,
        )
        write_log(log_path, f"Committed fix: {message}")
    except Exception as exc:
        write_log(log_path, f"⚠ commit skipped: {exc}")


def fix_off_by_one(workdir: Path, log_path: Path) -> str | None:
    """Fix ITEMS[n] -> ITEMS[n - 1] off-by-one in app.py."""
    app_file = workdir / "app.py"
    if not app_file.exists():
        return None
    content = app_file.read_text()
    fixed = content.replace(
        "return jsonify({\"id\": n, \"item\": ITEMS[n]})  # off-by-one",
        (
            "if n < 1 or n > len(ITEMS):\n"
            "        from flask import abort\n"
            "        abort(404)\n"
            "    return jsonify({\"id\": n, \"item\": ITEMS[n - 1]})"
        ),
    )
    if fixed != content:
        app_file.write_text(fixed)
        write_log(log_path, "✓ Fixed off-by-one: ITEMS[n] → ITEMS[n - 1] + bounds check")
        return "app.py"
    return None


def fix_missing_import(workdir: Path, log_path: Path) -> str | None:
    """Add 'request' to the flask import line in app.py."""
    app_file = workdir / "app.py"
    if not app_file.exists():
        return None
    content = app_file.read_text()
    old_import = "from flask import Flask, jsonify  # BUG 2: 'request' is missing from this import"
    new_import = "from flask import Flask, jsonify, request"
    fixed = content
    if old_import in content:
        fixed = fixed.replace(old_import, new_import)
        fixed = fixed.replace(
            "    msg = request.args.get(\"msg\", \"\")  # type: ignore[name-defined]  # noqa: F821",
            "    msg = request.args.get(\"msg\", \"\")",
        )
    elif "from flask import Flask, jsonify" in content and "request" not in content.split("\n")[1]:
        fixed = fixed.replace(
            "from flask import Flask, jsonify",
            "from flask import Flask, jsonify, request",
            1,
        )
    if fixed != content:
        app_file.write_text(fixed)
        write_log(log_path, "✓ Fixed missing import: added 'request' to flask imports")
        return "app.py"
    return None


def fix_health_status(workdir: Path, log_path: Path) -> str | None:
    """Remove incorrect HTTP 201 status from health endpoint in app.py."""
    app_file = workdir / "app.py"
    if not app_file.exists():
        return None
    content = app_file.read_text()
    old_line = '    return jsonify({"status": "healthy", "version": "1.0.0"}), 201  # type: ignore[return-value]'
    new_line = '    return jsonify({"status": "healthy", "version": "1.0.0"})'
    fixed = content.replace(old_line, new_line)
    if fixed != content:
        app_file.write_text(fixed)
        write_log(log_path, "✓ Fixed health status code: 201 → 200")
        return "app.py"
    return None


def fix_broken_test(workdir: Path, log_path: Path) -> str | None:
    """Fix the wrong status_code assertion in tests/test_app.py."""
    test_file = workdir / "tests" / "test_app.py"
    if not test_file.exists():
        return None
    content = test_file.read_text()
    fixed = content.replace(
        "assert resp.status_code == 404  # wrong - should be 200",
        "assert resp.status_code == 200",
    )
    fixed = fixed.replace(
        '\n    BUG 4: asserts 404 instead of 200.\n    ',
        '\n    ',
    )
    if fixed != content:
        test_file.write_text(fixed)
        write_log(log_path, "✓ Fixed broken test: status_code 404 → 200")
        return "tests/test_app.py"
    return None


def _idle_mode(log_path: Path) -> None:
    """Sleep for a randomized interval, optionally fail at the end."""
    import os
    import random

    def _int_env(key: str, default: int) -> int:
        raw = os.environ.get(key, "").strip()
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            write_log(log_path, f"idle: WARN bad {key}={raw!r}; using default {default}")
            return default

    def _float_env(key: str, default: float, *, invalid_default: float | None = None) -> float:
        raw = os.environ.get(key, "").strip()
        if not raw:
            return default
        try:
            return float(raw)
        except ValueError:
            fallback = default if invalid_default is None else invalid_default
            write_log(log_path, f"idle: WARN bad {key}={raw!r}; using default {fallback}")
            return fallback

    lo = _int_env("BERNSTEIN_MOCK_IDLE_MIN_S", 15)
    hi = _int_env("BERNSTEIN_MOCK_IDLE_MAX_S", 120)
    lo = max(0, lo)
    hi = max(0, hi)
    if hi < lo:
        lo, hi = hi, lo
    fail_rate = _float_env("BERNSTEIN_MOCK_FAIL_RATE", 0.05, invalid_default=0.0)
    will_fail = random.random() < fail_rate
    sleep_s = random.randint(lo, hi) if hi > 0 else 0

    write_log(log_path, f"idle: sleeping {sleep_s}s (will_fail={will_fail})")
    chunk = max(1, sleep_s // 6)
    elapsed = 0
    while elapsed < sleep_s:
        time.sleep(min(chunk, sleep_s - elapsed))
        elapsed += chunk
        write_log(log_path, f"idle: heartbeat {elapsed}/{sleep_s}s")
    if will_fail:
        write_log(log_path, "idle: simulated failure - exiting non-zero")
        sys.exit(1)
    write_log(log_path, "idle: completed")


def main():
    """Main entry point."""
    import os

    task_info = json.loads(sys.argv[1])
    workdir = Path(task_info["workdir"])
    task_id = task_info.get("task_id", "unknown")
    task_title = task_info.get("task_title", "")
    log_path = Path(task_info["log_path"])

    # Write the real task_id to the log so the reaper can attribute evidence
    # without parsing prompt text (issue #3629).
    write_log(log_path, f"TaskID: {task_id}")

    if os.environ.get("BERNSTEIN_MOCK_IDLE") == "1":
        _idle_mode(log_path)
        return

    time.sleep(1.5)

    # Determine which fix to apply based on the task title (unique per task)
    # rather than fragile prompt substring matching (issue #3629).
    title_lower = task_title.lower()
    fix_name = "unknown"
    if "off-by-one" in title_lower:
        fix_name = "off_by_one"
    elif "missing" in title_lower and "import" in title_lower:
        fix_name = "missing_import"
    elif "health" in title_lower:
        fix_name = "health_status"
    elif "broken" in title_lower or "assertion" in title_lower:
        fix_name = "broken_test"

    modified = None
    if fix_name == "off_by_one":
        modified = fix_off_by_one(workdir, log_path)
    elif fix_name == "missing_import":
        modified = fix_missing_import(workdir, log_path)
    elif fix_name == "health_status":
        modified = fix_health_status(workdir, log_path)
    elif fix_name == "broken_test":
        modified = fix_broken_test(workdir, log_path)
    else:
        write_log(log_path, f"Unknown task title: {task_title} - no-op")

    if modified is not None:
        commit_fix(workdir, log_path, f"demo: {fix_name.replace('_', ' ')}", modified)
        record_modified(log_path, modified)

    time.sleep(0.5)
    write_log(log_path, "Mock agent completed successfully")


if __name__ == "__main__":
    main()
'''
