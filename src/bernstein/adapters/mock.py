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


def _matches_off_by_one(prompt_lower: str) -> bool:
    """Check if prompt matches off-by-one task patterns."""
    if "off-by-one" in prompt_lower or "off_by_one" in prompt_lower:
        return True
    return "items" in prompt_lower and (
        "index" in prompt_lower or "route" in prompt_lower or "n - 1" in prompt_lower or "1-indexed" in prompt_lower
    )


def _matches_missing_import(prompt_lower: str) -> bool:
    """Check if prompt matches missing-import task patterns."""
    return (
        "missing import" in prompt_lower
        or "missing `request`" in prompt_lower
        or ("request" in prompt_lower and "import" in prompt_lower)
    )


def _matches_health_status(prompt_lower: str) -> bool:
    """Check if prompt matches health-status task patterns."""
    return "201" in prompt_lower or ("health" in prompt_lower and ("status" in prompt_lower or "code" in prompt_lower))


def _matches_broken_test(prompt_lower: str) -> bool:
    """Check if prompt matches broken-test task patterns."""
    return (
        "broken" in prompt_lower
        or "assertion" in prompt_lower
        or ("test" in prompt_lower and ("404" in prompt_lower or "wrong" in prompt_lower))
    )


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
    ) -> SpawnResult:
        """Spawn a mock agent subprocess that applies demo changes.

        Args:
            prompt: Agent task description (analyzed to determine action).
            workdir: Project root directory.
            model_config: Model configuration (unused for mock).
            session_id: Unique session identifier.
            mcp_config: MCP configuration (unused for mock).

        Returns:
            SpawnResult with mock process PID and log path.
        """
        # Create log file
        self.refuse_multimodal_if_needed(multimodal_context)
        log_path = workdir / ".sdd" / "runtime" / f"agent-{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        # Determine which task this is based on the prompt content
        task_name = self._identify_task(prompt)

        # Create a temporary Python script that will simulate the agent work
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

        # Pass task info as JSON to avoid shell quoting issues
        task_info = json.dumps(
            {
                "workdir": str(workdir),
                "task_name": task_name,
                "log_path": str(log_path),
            }
        )

        cmd = [
            sys.executable,
            script_path,
            task_info,
        ]

        # Pass an explicit ``env=`` (allowlist only) so the mock adapter
        # cannot leak orchestrator credentials to the child python script.
        # The mock only needs PATH/HOME/PYTHONPATH which are already on
        # the base allowlist; the BERNSTEIN_MOCK_* vars opt the embedded
        # script into idle mode (used by ``bernstein run --idle`` for GUI
        # development).
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
    def _identify_task(prompt: str) -> str:
        """Identify which task this is from the prompt text.

        Args:
            prompt: Agent task description.

        Returns:
            Task identifier matching one of the fix functions in the mock script.
        """
        prompt_lower = prompt.lower()
        if _matches_off_by_one(prompt_lower):
            return "off_by_one"
        if _matches_missing_import(prompt_lower):
            return "missing_import"
        if _matches_health_status(prompt_lower):
            return "health_status"
        if _matches_broken_test(prompt_lower):
            return "broken_test"
        # Legacy / generic fallbacks
        if "health" in prompt_lower or "/health" in prompt_lower:
            return "health_status"
        if "test" in prompt_lower:
            return "broken_test"
        if "error" in prompt_lower or "handler" in prompt_lower:
            return "off_by_one"
        return "unknown"

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


def fix_off_by_one(workdir: Path, log_path: Path) -> None:
    """Fix ITEMS[n] -> ITEMS[n - 1] off-by-one in app.py."""
    app_file = workdir / "app.py"
    if not app_file.exists():
        write_log(log_path, "⚠ app.py not found")
        return
    content = app_file.read_text()
    if "ITEMS[n]" in content:
        content = content.replace(
            "return jsonify({\"id\": n, \"item\": ITEMS[n]})  # off-by-one",
            (
                "if n < 1 or n > len(ITEMS):\n"
                "        from flask import abort\n"
                "        abort(404)\n"
                "    return jsonify({\"id\": n, \"item\": ITEMS[n - 1]})"
            ),
        )
        app_file.write_text(content)
        write_log(log_path, "✓ Fixed off-by-one: ITEMS[n] → ITEMS[n - 1] + bounds check")
    else:
        write_log(log_path, "⚠ off-by-one pattern not found (already fixed?)")


def fix_missing_import(workdir: Path, log_path: Path) -> None:
    """Add 'request' to the flask import line in app.py."""
    app_file = workdir / "app.py"
    if not app_file.exists():
        write_log(log_path, "⚠ app.py not found")
        return
    content = app_file.read_text()
    old_import = "from flask import Flask, jsonify  # BUG 2: 'request' is missing from this import"
    new_import = "from flask import Flask, jsonify, request"
    if old_import in content:
        content = content.replace(old_import, new_import)
        # Also remove the noqa/type-ignore comment from the echo route
        content = content.replace(
            "    msg = request.args.get(\"msg\", \"\")  # type: ignore[name-defined]  # noqa: F821",
            "    msg = request.args.get(\"msg\", \"\")",
        )
        app_file.write_text(content)
        write_log(log_path, "✓ Fixed missing import: added 'request' to flask imports")
    elif "from flask import Flask, jsonify" in content and "request" not in content.split("\n")[1]:
        content = content.replace(
            "from flask import Flask, jsonify",
            "from flask import Flask, jsonify, request",
            1,
        )
        app_file.write_text(content)
        write_log(log_path, "✓ Fixed missing import: added 'request' to flask imports")
    else:
        write_log(log_path, "⚠ missing import pattern not found (already fixed?)")


def fix_health_status(workdir: Path, log_path: Path) -> None:
    """Remove incorrect HTTP 201 status from health endpoint in app.py."""
    app_file = workdir / "app.py"
    if not app_file.exists():
        write_log(log_path, "⚠ app.py not found")
        return
    content = app_file.read_text()
    old_line = '    return jsonify({"status": "healthy", "version": "1.0.0"}), 201  # type: ignore[return-value]'
    new_line = '    return jsonify({"status": "healthy", "version": "1.0.0"})'
    if old_line in content:
        content = content.replace(old_line, new_line)
        app_file.write_text(content)
        write_log(log_path, "✓ Fixed health status code: 201 → 200")
    else:
        write_log(log_path, "⚠ health status code pattern not found (already fixed?)")


def fix_broken_test(workdir: Path, log_path: Path) -> None:
    """Fix the wrong status_code assertion in tests/test_app.py."""
    test_file = workdir / "tests" / "test_app.py"
    if not test_file.exists():
        write_log(log_path, "⚠ tests/test_app.py not found")
        return
    content = test_file.read_text()
    if "assert resp.status_code == 404  # wrong - should be 200" in content:
        content = content.replace(
            "assert resp.status_code == 404  # wrong - should be 200",
            "assert resp.status_code == 200",
        )
        # Also remove the BUG 4 docstring annotation
        content = content.replace(
            '\n    BUG 4: asserts 404 instead of 200.\n    ',
            '\n    ',
        )
        test_file.write_text(content)
        write_log(log_path, "✓ Fixed broken test: status_code 404 → 200")
    else:
        write_log(log_path, "⚠ broken test pattern not found (already fixed?)")


def _idle_mode(log_path: Path) -> None:
    """Sleep for a randomized interval, optionally fail at the end.

    Driven by ``BERNSTEIN_MOCK_IDLE_MIN_S`` (default 15) and
    ``BERNSTEIN_MOCK_IDLE_MAX_S`` (default 120). With probability
    ``BERNSTEIN_MOCK_FAIL_RATE`` (default 0.05) the agent exits non-zero so
    the GUI shows a mix of completed/failed states.

    Env vars that fail to parse fall back to defaults instead of crashing
    so a typo (e.g. ``BERNSTEIN_MOCK_IDLE_MIN_S=180s``) does not silently
    abort GUI demo agents.
    """
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
    # Clamp to non-negative to avoid random.randint(0, 0) edge crashes.
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
    task_name = task_info["task_name"]
    log_path = Path(task_info["log_path"])

    write_log(log_path, f"Mock agent started for task: {task_name}")

    # Idle mode: GUI dev path - `bernstein run --idle` sets BERNSTEIN_MOCK_IDLE=1
    # so each spawned mock just sleeps + emits heartbeat lines instead of doing fixes.
    if os.environ.get("BERNSTEIN_MOCK_IDLE") == "1":
        _idle_mode(log_path)
        return

    # Simulate realistic agent work time
    time.sleep(1.5)

    if task_name == "off_by_one":
        fix_off_by_one(workdir, log_path)
    elif task_name == "missing_import":
        fix_missing_import(workdir, log_path)
    elif task_name == "health_status":
        fix_health_status(workdir, log_path)
    elif task_name == "broken_test":
        fix_broken_test(workdir, log_path)
    else:
        write_log(log_path, f"Unknown task type: {task_name} - no-op")

    time.sleep(0.5)
    write_log(log_path, "Mock agent completed successfully")


if __name__ == "__main__":
    main()
'''
