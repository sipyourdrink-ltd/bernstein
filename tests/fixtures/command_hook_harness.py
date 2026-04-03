"""Fixture harness for testing Bernstein command hook scripts end-to-end (T601).

This harness exercises the full hook protocol — JSON-over-stdin, BERNSTEIN_HOOK_*
environment variables, exit-code semantics, and JSON stdout responses — without
requiring a running Bernstein orchestrator or real agent.

Usage::

    from tests.fixtures.command_hook_harness import CommandHookHarness

    def test_hook_receives_task_id(tmp_path):
        harness = CommandHookHarness(tmp_path / "hooks")
        harness.add_capture_script("on_task_created")
        harness.fire("on_task_created", task_id="T-001", role="backend", title="My Task")
        captured = harness.read_captured("on_task_created")
        assert captured["stdin"]["task_id"] == "T-001"
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from bernstein.plugins.manager import CommandHook

# ---------------------------------------------------------------------------
# Script templates — written as inline Python so no external shell dependency
# ---------------------------------------------------------------------------

# Script that writes {stdin: <parsed JSON>, env: {BERNSTEIN_HOOK_*}} to a
# capture file next to the script itself.
_CAPTURE_SCRIPT_TEMPLATE = """\
#!/usr/bin/env {python}
import json
import os
import sys

stdin_text = sys.stdin.read()
try:
    stdin_data = json.loads(stdin_text)
except Exception:
    stdin_data = stdin_text

env_data = {{k: v for k, v in os.environ.items() if k.startswith("BERNSTEIN_HOOK_")}}
capture = {{"stdin": stdin_data, "env": env_data}}

capture_path = {capture_path!r}
with open(capture_path, "w") as _f:
    json.dump(capture, _f)

print('{{"status": "ok"}}')
"""

# Script that exits with a fixed exit code, optionally printing to stderr.
_EXIT_SCRIPT_TEMPLATE = """\
#!/usr/bin/env {python}
import sys
stderr_msg = {stderr!r}
if stderr_msg:
    print(stderr_msg, file=sys.stderr)
sys.exit({exit_code})
"""

# Script that prints a JSON response to stdout and exits 0.
_JSON_RESPONSE_SCRIPT_TEMPLATE = """\
#!/usr/bin/env {python}
import json
import sys
print(json.dumps({response!r}))
sys.exit(0)
"""


class CommandHookHarness:
    """Test harness for exercising :class:`~bernstein.plugins.manager.CommandHook`.

    Creates temporary hook-script directories, provides helpers to add scripts
    with controlled behavior, invokes hooks via the real ``CommandHook``
    implementation, and reads capture files written by capture scripts.

    Args:
        hooks_dir: Root directory for hook scripts.  Created lazily on first
            ``add_*`` call.  Typically ``tmp_path / "hooks"`` in pytest tests.
    """

    def __init__(self, hooks_dir: Path) -> None:
        self._hooks_dir = hooks_dir
        self._hook = CommandHook(hooks_dir)
        # Track capture file paths so read_captured() can find them.
        self._capture_paths: dict[tuple[str, str], Path] = {}

    # ------------------------------------------------------------------
    # Script builders
    # ------------------------------------------------------------------

    def add_capture_script(
        self,
        hook_name: str,
        script_name: str = "capture.py",
    ) -> Path:
        """Add a hook script that records its stdin and env to a capture file.

        The capture file is a JSON object with two keys:
        - ``stdin``: the parsed JSON payload written to the script's stdin.
        - ``env``: a dict of all ``BERNSTEIN_HOOK_*`` environment variables.

        Args:
            hook_name: Hook event name (e.g. ``"on_task_created"``).
            script_name: Filename for the hook script.

        Returns:
            Path to the created hook script.
        """
        hook_dir = self._hooks_dir / hook_name
        hook_dir.mkdir(parents=True, exist_ok=True)
        script = hook_dir / script_name
        capture_path = hook_dir / f"{script_name}.capture.json"
        self._capture_paths[(hook_name, script_name)] = capture_path

        script.write_text(
            _CAPTURE_SCRIPT_TEMPLATE.format(
                python=sys.executable,
                capture_path=str(capture_path),
            ),
            encoding="utf-8",
        )
        script.chmod(0o755)
        return script

    def add_exit_script(
        self,
        hook_name: str,
        exit_code: int,
        stderr: str = "",
        script_name: str = "exit.py",
    ) -> Path:
        """Add a hook script that exits with *exit_code*.

        Useful for testing exit-code semantics:
        - ``exit_code=0``: success (no-op in orchestrator).
        - ``exit_code=2``: blocking error → raises
          :class:`~bernstein.plugins.manager.HookBlockingError`.
        - any other non-zero: warning logged, orchestration continues.

        Args:
            hook_name: Hook event name.
            exit_code: Exit code the script will use.
            stderr: Optional message printed to stderr before exiting.
            script_name: Filename for the hook script.

        Returns:
            Path to the created hook script.
        """
        hook_dir = self._hooks_dir / hook_name
        hook_dir.mkdir(parents=True, exist_ok=True)
        script = hook_dir / script_name
        script.write_text(
            _EXIT_SCRIPT_TEMPLATE.format(
                python=sys.executable,
                exit_code=exit_code,
                stderr=stderr,
            ),
            encoding="utf-8",
        )
        script.chmod(0o755)
        return script

    def add_json_response_script(
        self,
        hook_name: str,
        response: dict[str, Any],
        script_name: str = "respond.py",
    ) -> Path:
        """Add a hook script that prints *response* as JSON to stdout and exits 0.

        Args:
            hook_name: Hook event name.
            response: JSON-serialisable dict to print.
            script_name: Filename for the hook script.

        Returns:
            Path to the created hook script.
        """
        hook_dir = self._hooks_dir / hook_name
        hook_dir.mkdir(parents=True, exist_ok=True)
        script = hook_dir / script_name
        script.write_text(
            _JSON_RESPONSE_SCRIPT_TEMPLATE.format(
                python=sys.executable,
                response=response,
            ),
            encoding="utf-8",
        )
        script.chmod(0o755)
        return script

    # ------------------------------------------------------------------
    # Invocation
    # ------------------------------------------------------------------

    def fire(self, hook_name: str, **kwargs: Any) -> None:
        """Invoke *hook_name* via the real :class:`CommandHook` implementation.

        This calls ``CommandHook._run_command`` directly, which handles stdin
        serialisation, env-var injection, exit-code checking, and dedup.

        Args:
            hook_name: Hook event name (e.g. ``"on_task_created"``).
            **kwargs: Hook arguments forwarded as-is.

        Raises:
            HookBlockingError: If a registered script exits with code 2.
        """
        self._hook._run_command(hook_name, **kwargs)

    # ------------------------------------------------------------------
    # Assertions / result reading
    # ------------------------------------------------------------------

    def read_captured(
        self,
        hook_name: str,
        script_name: str = "capture.py",
    ) -> dict[str, Any]:
        """Read the capture file written by a capture script.

        Args:
            hook_name: Hook event name.
            script_name: Script name passed to :meth:`add_capture_script`.

        Returns:
            Dict with ``"stdin"`` (parsed JSON payload) and ``"env"``
            (``BERNSTEIN_HOOK_*`` variables) keys.

        Raises:
            FileNotFoundError: If no capture script was added for this hook, or
                the hook was never fired.
        """
        key = (hook_name, script_name)
        if key not in self._capture_paths:
            raise FileNotFoundError(
                f"No capture script registered for hook {hook_name!r} / {script_name!r}. "
                "Did you call add_capture_script() first?"
            )
        capture_path = self._capture_paths[key]
        if not capture_path.exists():
            raise FileNotFoundError(
                f"Capture file {capture_path} does not exist. "
                f"Was fire({hook_name!r}, ...) called?"
            )
        return dict(json.loads(capture_path.read_text(encoding="utf-8")))
