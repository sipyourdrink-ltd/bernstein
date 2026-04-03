"""Plugin manager — discovers, loads, and invokes Bernstein plugins."""

from __future__ import annotations

import importlib
import json
import logging
import os
import subprocess
import time
import warnings
from concurrent.futures import ThreadPoolExecutor
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, cast

import pluggy

from bernstein.core.workspace import is_workspace_trusted
from bernstein.plugins import hookimpl
from bernstein.plugins.hookspecs import BernsteinSpec

log = logging.getLogger(__name__)

__all__ = ["SLOW_HOOK_THRESHOLD", "HookBlockingError", "PluginManager", "get_plugin_manager"]

# Threshold in seconds above which a hook execution is logged as "slow".
SLOW_HOOK_THRESHOLD: float = 1.0


# Module-level singleton so the same manager is reused within a process.
_manager: PluginManager | None = None


class HookBlockingError(Exception):
    """Raised when a hook command exits with code 2, indicating a blocking failure."""

    def __init__(self, hook_name: str, stderr: str) -> None:
        super().__init__(f"Hook {hook_name!r} blocked orchestration: {stderr}")
        self.hook_name = hook_name
        self.stderr = stderr


class CommandHook:
    """A plugin that executes shell scripts for hooks.

    Discovered in ``.bernstein/hooks/<hook_name>/`` as executable files.

    Each script is tracked by its resolved path so that the same hook script
    is only executed once even if registered by multiple plugin sources (T455).
    """

    def __init__(
        self,
        hooks_dir: Path,
        plugin_root: str = "",
        seen: set[tuple[str, str]] | None = None,
    ) -> None:
        """Create a CommandHook instance.

        Args:
            hooks_dir: Directory tree containing hook script subdirectories.
            plugin_root: Dotted import path or name identifying the plugin
                source. Used for dedup logging when collisions occur.
            seen: Shared set tracking registered hook+script combos across all
                CommandHook instances. Mutated in place.
        """
        self._hooks_dir = hooks_dir
        self._plugin_root = plugin_root
        self._seen: set[tuple[str, str]] = seen if seen is not None else set()

    def _script_key(self, script: Path) -> str:
        """Return a dedup key for a script based on its resolved path."""
        return str(script.resolve())

    def _is_duplicate(self, hook_name: str, script: Path) -> bool:
        """Check and record a hook+script registration.

        Returns True if this combination was already seen (skip execution).
        First registration wins; subsequent calls with the same key log
        and are skipped.
        """
        key = (hook_name, self._script_key(script))
        if key in self._seen:
            return True
        self._seen.add(key)
        return False

    def _run_command(self, hook_name: str, **kwargs: Any) -> None:
        hook_path = self._hooks_dir / hook_name
        if not hook_path.is_dir():
            return

        # Find all executable files in the directory
        for script in sorted(hook_path.iterdir()):
            if not os.access(script, os.X_OK) or script.is_dir():
                continue

            # Deduplicate: skip scripts already registered (T455)
            if self._is_duplicate(hook_name, script):
                log.debug(
                    "Skipping duplicate hook %s/%s (already registered via %s)",
                    hook_name,
                    script.name,
                    self._plugin_root,
                )
                continue

            log.debug("Executing hook script: %s", script)
            try:
                # Pass arguments via environment variables
                env = os.environ.copy()
                for key, value in kwargs.items():
                    env[f"BERNSTEIN_HOOK_{key.upper()}"] = str(value)

                # Also pass as JSON via stdin
                proc = subprocess.run(
                    [str(script)],
                    input=json.dumps(kwargs),
                    text=True,
                    capture_output=True,
                    env=env,
                    check=False,
                )

                if proc.returncode == 0:
                    # Parse JSON response from stdout if present
                    if proc.stdout.strip():
                        try:
                            response = cast("dict[str, Any]", json.loads(proc.stdout))
                            status = str(response.get("status", ""))
                            message = str(response.get("message", ""))
                            if status == "error":
                                log.warning(
                                    "Hook script %s reported error: %s",
                                    script.name,
                                    message or "no message",
                                )
                        except (json.JSONDecodeError, TypeError):
                            log.warning(
                                "Hook script %s returned malformed JSON: %s",
                                script.name,
                                proc.stdout[:100],
                            )
                    continue

                if proc.returncode == 2:
                    error_detail: str = proc.stderr.strip() or proc.stdout.strip()
                    # Try to extract message from JSON if possible
                    if proc.stdout.strip():
                        try:
                            response = cast("dict[str, Any]", json.loads(proc.stdout))
                            if response.get("message"):
                                error_detail = str(response["message"])
                        except (json.JSONDecodeError, TypeError):
                            pass
                    raise HookBlockingError(hook_name, error_detail)
                else:
                    log.warning(
                        "Hook script %s exited with code %d: %s",
                        script.name,
                        proc.returncode,
                        proc.stderr.strip() or proc.stdout.strip(),
                    )
            except HookBlockingError:
                raise
            except Exception as exc:
                log.warning("Failed to execute hook script %s: %s", script, exc)

    @hookimpl
    def on_task_created(self, task_id: str, role: str, title: str) -> None:
        self._run_command("on_task_created", task_id=task_id, role=role, title=title)

    @hookimpl
    def on_task_completed(self, task_id: str, role: str, result_summary: str) -> None:
        self._run_command("on_task_completed", task_id=task_id, role=role, result_summary=result_summary)

    @hookimpl
    def on_task_failed(self, task_id: str, role: str, error: str) -> None:
        self._run_command("on_task_failed", task_id=task_id, role=role, error=error)

    @hookimpl
    def on_agent_spawned(self, session_id: str, role: str, model: str) -> None:
        self._run_command("on_agent_spawned", session_id=session_id, role=role, model=model)

    @hookimpl
    def on_agent_reaped(self, session_id: str, role: str, outcome: str) -> None:
        self._run_command("on_agent_reaped", session_id=session_id, role=role, outcome=outcome)

    @hookimpl
    def on_tool_error(self, session_id: str, tool: str, error: str, batch_id: str | None = None) -> None:
        self._run_command("on_tool_error", session_id=session_id, tool=tool, error=error, batch_id=batch_id)

    @hookimpl
    def on_evolve_proposal(self, proposal_id: str, title: str, verdict: str) -> None:
        self._run_command("on_evolve_proposal", proposal_id=proposal_id, title=title, verdict=verdict)

    @hookimpl
    def on_pre_task_create(
        self,
        task_id: str,
        role: str,
        title: str,
        description: str,
    ) -> None:
        self._run_command(
            "on_pre_task_create",
            task_id=task_id,
            role=role,
            title=title,
            description=description,
        )

    @hookimpl
    def on_permission_denied(self, task_id: str, reason: str, tool: str, args: dict[str, Any]) -> str | None:
        # Command hooks can't easily return a value to firstresult=True
        # because the CommandHook wrapper currently returns None.
        # For now, just log it. In a future iteration, we might parse stdout
        # from the script to get a hint.
        self._run_command("on_permission_denied", task_id=task_id, reason=reason, tool=tool, **args)
        return None

    # --- Additional hooks for T681 (22 new lifecycle events) ---

    @hookimpl
    def on_pre_tool_use(
        self,
        session_id: str,
        tool: str,
        tool_input: dict[str, Any],
    ) -> str | None:
        self._run_command("on_pre_tool_use", session_id=session_id, tool=tool, tool_input=tool_input)
        return None

    @hookimpl
    def on_post_tool_use(
        self,
        session_id: str,
        tool: str,
        tool_input: dict[str, Any],
        result: str,
        success: bool,
    ) -> None:
        self._run_command(
            "on_post_tool_use",
            session_id=session_id,
            tool=tool,
            tool_input=tool_input,
            result=result,
            success=success,
        )

    @hookimpl
    def on_post_tool_use_failure(
        self,
        session_id: str,
        tool: str,
        tool_input: dict[str, Any],
        error: str,
        retries: int,
    ) -> None:
        self._run_command(
            "on_post_tool_use_failure",
            session_id=session_id,
            tool=tool,
            tool_input=tool_input,
            error=error,
            retries=retries,
        )

    @hookimpl
    def on_notification(self, session_id: str, level: str, message: str) -> None:
        self._run_command("on_notification", session_id=session_id, level=level, message=message)

    @hookimpl
    def on_user_prompt_submit(self, session_id: str, prompt: str) -> None:
        self._run_command("on_user_prompt_submit", session_id=session_id, prompt=prompt)

    @hookimpl
    def on_session_start(self, session_id: str, role: str, task_id: str) -> None:
        self._run_command("on_session_start", session_id=session_id, role=role, task_id=task_id)

    @hookimpl
    def on_session_end(self, session_id: str, role: str, reason: str) -> None:
        self._run_command("on_session_end", session_id=session_id, role=role, reason=reason)

    @hookimpl
    def on_stop(self, session_id: str, reason: str, signal: str = "SIGTERM") -> None:
        self._run_command("on_stop", session_id=session_id, reason=reason, signal=signal)

    @hookimpl
    def on_stop_failure(self, session_id: str, reason: str, error: str) -> None:
        self._run_command("on_stop_failure", session_id=session_id, reason=reason, error=error)

    @hookimpl
    def on_subagent_start(self, session_id: str, sub_id: str, role: str) -> None:
        self._run_command("on_subagent_start", session_id=session_id, sub_id=sub_id, role=role)

    @hookimpl
    def on_subagent_stop(self, session_id: str, sub_id: str, outcome: str) -> None:
        self._run_command("on_subagent_stop", session_id=session_id, sub_id=sub_id, outcome=outcome)

    @hookimpl
    def on_permission_request(self, session_id: str, tool: str, mode: str) -> None:
        self._run_command("on_permission_request", session_id=session_id, tool=tool, mode=mode)

    @hookimpl
    def on_setup(self, session_id: str, role: str, workdir: str) -> None:
        self._run_command("on_setup", session_id=session_id, role=role, workdir=workdir)

    @hookimpl
    def on_teammate_idle(self, session_id: str, role: str, queue_depth: int) -> None:
        self._run_command("on_teammate_idle", session_id=session_id, role=role, queue_depth=queue_depth)

    @hookimpl
    def on_elicitation(self, session_id: str, prompt: str, options: list[str]) -> None:
        self._run_command("on_elicitation", session_id=session_id, prompt=prompt, options=options)

    @hookimpl
    def on_elicitation_result(self, session_id: str, prompt: str, response: str) -> None:
        self._run_command("on_elicitation_result", session_id=session_id, prompt=prompt, response=response)

    @hookimpl
    def on_config_change(self, key: str, old_value: str, new_value: str) -> None:
        self._run_command("on_config_change", key=key, old_value=old_value, new_value=new_value)

    @hookimpl
    def on_worktree_create(self, session_id: str, worktree_path: str, branch: str) -> None:
        self._run_command(
            "on_worktree_create",
            session_id=session_id,
            worktree_path=worktree_path,
            branch=branch,
        )

    @hookimpl
    def on_worktree_remove(self, session_id: str, worktree_path: str) -> None:
        self._run_command("on_worktree_remove", session_id=session_id, worktree_path=worktree_path)

    @hookimpl
    def on_instructions_loaded(self, session_id: str, role: str, source_paths: list[str]) -> None:
        self._run_command(
            "on_instructions_loaded",
            session_id=session_id,
            role=role,
            source_paths=source_paths,
        )

    @hookimpl
    def on_cwd_changed(self, session_id: str, old_cwd: str, new_cwd: str) -> None:
        self._run_command("on_cwd_changed", session_id=session_id, old_cwd=old_cwd, new_cwd=new_cwd)

    @hookimpl
    def on_file_changed(self, session_id: str, file_path: str, change_type: str) -> None:
        self._run_command(
            "on_file_changed",
            session_id=session_id,
            file_path=file_path,
            change_type=change_type,
        )


class PluginManager:
    """Discovers, loads, and invokes Bernstein plugins.

    Plugins are discovered from two sources:

    1. **Entry points** — any installed package that registers hooks under
       the ``bernstein.plugins`` entry-point group.
    2. **bernstein.yaml** ``plugins:`` field — a list of dotted import paths
       to be imported and registered as plugins.

    The manager handles lifecycle hooks (task creation, agent spawning, etc.)
    and provides a thread pool for background hooks.
    """

    def __init__(self, workdir: Path | None = None) -> None:
        self._pm = pluggy.PluginManager("bernstein")
        self._pm.add_hookspecs(BernsteinSpec)
        self._registered_names: list[str] = []
        self._workdir = workdir
        # Use a small pool for background hooks.
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="BernsteinPluginHook")
        # Shared dedup registry for hook scripts across all CommandHook instances (T455).
        self._hook_seen: set[tuple[str, str]] = set()

    def fire_task_created(self, task_id: str, role: str, title: str) -> None:
        self._safe_call("on_task_created", task_id=task_id, role=role, title=title)

    def fire_pre_task_create(self, task_id: str, role: str, title: str, description: str) -> None:
        """Fire pre-task-create hooks that can block the operation (T719).

        Args:
            task_id: Unique task identifier (generated before creation).
            role: Agent role assigned to the task.
            title: Human-readable task title.
            description: Task description text.

        Raises:
            HookBlockingError: If a hook exits with code 2, blocking creation.
        """
        self._safe_call(
            "on_pre_task_create",
            task_id=task_id,
            role=role,
            title=title,
            description=description,
        )

    def fire_task_completed(self, task_id: str, role: str, result_summary: str) -> None:
        self._safe_call("on_task_completed", task_id=task_id, role=role, result_summary=result_summary)

    def fire_task_failed(self, task_id: str, role: str, error: str) -> None:
        self._safe_call("on_task_failed", task_id=task_id, role=role, error=error)

    def fire_agent_spawned(self, session_id: str, role: str, model: str) -> None:
        self._safe_call("on_agent_spawned", session_id=session_id, role=role, model=model)

    def fire_agent_reaped(self, session_id: str, role: str, outcome: str) -> None:
        self._safe_call("on_agent_reaped", session_id=session_id, role=role, outcome=outcome)

    def fire_evolve_proposal(self, proposal_id: str, title: str, verdict: str) -> None:
        self._safe_call("on_evolve_proposal", proposal_id=proposal_id, title=title, verdict=verdict)

    def fire_permission_denied(self, task_id: str, reason: str, tool: str, args: dict[str, Any]) -> str | None:
        """Fire on_permission_denied hook and return first non-None hint."""
        if not self._check_workspace_trust():
            return None
        try:
            return self._pm.hook.on_permission_denied(task_id=task_id, reason=reason, tool=tool, args=args)
        except Exception as exc:
            log.warning("on_permission_denied hook failed: %s", exc)
            return None

    def fire_tool_error(self, session_id: str, tool: str, error: str, batch_id: str | None = None) -> None:
        """Fire on_tool_error hook."""
        self._safe_call("on_tool_error", session_id=session_id, tool=tool, error=error, batch_id=batch_id)

    # --- Fire methods for T681 lifecycle event hooks ---

    def fire_pre_tool_use(
        self,
        session_id: str,
        tool: str,
        tool_input: dict[str, Any],
    ) -> str | None:
        """Fire on_pre_tool_use hook — can block via HookBlockingError (T681).

        Returns:
            Optional denial hint from a firstresult hook, or None.
        """
        try:
            return self._pm.hook.on_pre_tool_use(
                session_id=session_id,
                tool=tool,
                tool_input=tool_input,
            )
        except HookBlockingError:
            raise
        except Exception as exc:
            log.warning("on_pre_tool_use hook failed: %s", exc)
            return None

    def fire_post_tool_use(
        self,
        session_id: str,
        tool: str,
        tool_input: dict[str, Any],
        result: str,
        success: bool,
    ) -> None:
        """Fire on_post_tool_use hook after tool execution (T681)."""
        self._safe_call(
            "on_post_tool_use",
            session_id=session_id,
            tool=tool,
            tool_input=tool_input,
            result=result,
            success=success,
        )

    def fire_post_tool_use_failure(
        self,
        session_id: str,
        tool: str,
        tool_input: dict[str, Any],
        error: str,
        retries: int,
    ) -> None:
        """Fire on_post_tool_use_failure when a tool fails after retries (T681)."""
        self._safe_call(
            "on_post_tool_use_failure",
            session_id=session_id,
            tool=tool,
            tool_input=tool_input,
            error=error,
            retries=retries,
        )

    def fire_notification(self, session_id: str, level: str, message: str) -> None:
        """Fire on_notification hook for operator-visible events (T681)."""
        self._safe_call("on_notification", session_id=session_id, level=level, message=message)

    def fire_user_prompt_submit(self, session_id: str, prompt: str) -> None:
        """Fire on_user_prompt_submit hook (T681)."""
        self._safe_call("on_user_prompt_submit", session_id=session_id, prompt=prompt)

    def fire_session_start(self, session_id: str, role: str, task_id: str) -> None:
        """Fire on_session_start hook when an agent session begins (T681)."""
        self._safe_call("on_session_start", session_id=session_id, role=role, task_id=task_id)

    def fire_session_end(self, session_id: str, role: str, reason: str) -> None:
        """Fire on_session_end hook when an agent session terminates (T681)."""
        self._safe_call("on_session_end", session_id=session_id, role=role, reason=reason)

    def fire_stop(self, session_id: str, reason: str, signal: str = "SIGTERM") -> None:
        """Fire on_stop hook when a stop/shutdown is initiated (T681)."""
        self._safe_call("on_stop", session_id=session_id, reason=reason, signal=signal)

    def fire_stop_failure(self, session_id: str, reason: str, error: str) -> None:
        """Fire on_stop_failure hook when a stop attempt fails (T681)."""
        self._safe_call("on_stop_failure", session_id=session_id, reason=reason, error=error)

    def fire_subagent_start(self, session_id: str, sub_id: str, role: str) -> None:
        """Fire on_subagent_start hook when spawning a sub-agent (T681)."""
        self._safe_call("on_subagent_start", session_id=session_id, sub_id=sub_id, role=role)

    def fire_subagent_stop(self, session_id: str, sub_id: str, outcome: str) -> None:
        """Fire on_subagent_stop hook when a sub-agent ends (T681)."""
        self._safe_call("on_subagent_stop", session_id=session_id, sub_id=sub_id, outcome=outcome)

    def fire_permission_request(self, session_id: str, tool: str, mode: str) -> None:
        """Fire on_permission_request hook before permission resolution (T681)."""
        self._safe_call("on_permission_request", session_id=session_id, tool=tool, mode=mode)

    def fire_setup(self, session_id: str, role: str, workdir: str) -> None:
        """Fire on_setup hook during workspace/worktree setup (T681)."""
        self._safe_call("on_setup", session_id=session_id, role=role, workdir=workdir)

    def fire_teammate_idle(self, session_id: str, role: str, queue_depth: int) -> None:
        """Fire on_teammate_idle hook when an agent has no more work (T681)."""
        self._safe_call("on_teammate_idle", session_id=session_id, role=role, queue_depth=queue_depth)

    def fire_elicitation(self, session_id: str, prompt: str, options: list[str]) -> None:
        """Fire on_elicitation hook when an LLM requests human input (T681)."""
        self._safe_call("on_elicitation", session_id=session_id, prompt=prompt, options=options)

    def fire_elicitation_result(self, session_id: str, prompt: str, response: str) -> None:
        """Fire on_elicitation_result after human input is provided (T681)."""
        self._safe_call("on_elicitation_result", session_id=session_id, prompt=prompt, response=response)

    def fire_config_change(self, key: str, old_value: str, new_value: str) -> None:
        """Fire on_config_change hook when a runtime config value changes (T681)."""
        self._safe_call("on_config_change", key=key, old_value=old_value, new_value=new_value)

    def fire_worktree_create(
        self,
        session_id: str,
        worktree_path: str,
        branch: str,
    ) -> None:
        """Fire on_worktree_create hook when a new worktree is created (T681)."""
        self._safe_call(
            "on_worktree_create",
            session_id=session_id,
            worktree_path=worktree_path,
            branch=branch,
        )

    def fire_worktree_remove(self, session_id: str, worktree_path: str) -> None:
        """Fire on_worktree_remove hook when a worktree is cleaned up (T681)."""
        self._safe_call("on_worktree_remove", session_id=session_id, worktree_path=worktree_path)

    def fire_instructions_loaded(
        self,
        session_id: str,
        role: str,
        source_paths: list[str],
    ) -> None:
        """Fire on_instructions_loaded hook after instruction files are read (T681)."""
        self._safe_call(
            "on_instructions_loaded",
            session_id=session_id,
            role=role,
            source_paths=source_paths,
        )

    def fire_cwd_changed(self, session_id: str, old_cwd: str, new_cwd: str) -> None:
        """Fire on_cwd_changed hook when the agent's working directory changes (T681)."""
        self._safe_call("on_cwd_changed", session_id=session_id, old_cwd=old_cwd, new_cwd=new_cwd)

    def fire_file_changed(self, session_id: str, file_path: str, change_type: str) -> None:
        """Fire on_file_changed hook when a worktree file changes (T681).

        Args:
            session_id: Agent session identifier.
            file_path: Path to the changed file.
            change_type: ``"created"``, ``"modified"``, or ``"deleted"``.
        """
        self._safe_call(
            "on_file_changed",
            session_id=session_id,
            file_path=file_path,
            change_type=change_type,
        )

    def discover_entry_points(self) -> None:
        """Load all plugins registered via the ``bernstein.plugins`` entry-point group."""
        eps = entry_points(group="bernstein.plugins")
        for ep in eps:
            try:
                plugin = ep.load()
                # Entry points may point to a class or an instance; instantiate if needed.
                if isinstance(plugin, type):
                    plugin = plugin()
                name = ep.name
                self._pm.register(plugin, name=name)
                self._registered_names.append(name)
                log.debug("Loaded entry-point plugin %r from %s", name, ep.value)
            except Exception as exc:
                warnings.warn(
                    f"Failed to load bernstein plugin {ep.name!r} ({ep.value}): {exc}",
                    stacklevel=1,
                )

    def discover_config_plugins(self, config_plugins: list[str]) -> None:
        """Load plugins listed in ``bernstein.yaml`` under the ``plugins:`` key.

        Each entry should be a dotted import path, optionally with a colon
        separating the module from the attribute, e.g.
        ``"my_package.hooks:MyPlugin"``.

        Args:
            config_plugins: List of import-path strings from the config file.
        """
        for spec in config_plugins:
            try:
                if ":" in spec:
                    module_path, attr = spec.rsplit(":", 1)
                    mod = importlib.import_module(module_path)
                    obj = getattr(mod, attr)
                else:
                    mod = importlib.import_module(spec)
                    obj = mod

                plugin = obj() if isinstance(obj, type) else obj
                name = spec
                self._pm.register(plugin, name=name)
                self._registered_names.append(name)
                log.debug("Loaded config plugin %r", name)
            except Exception as exc:
                warnings.warn(
                    f"Failed to load bernstein config plugin {spec!r}: {exc}",
                    stacklevel=1,
                )

    def load_from_workdir(self, workdir: Path | None = None) -> None:
        """Convenience: discover entry points then load any config-listed plugins.

        Reads ``plugins:`` from ``bernstein.yaml`` in *workdir* (or the current
        directory if *workdir* is ``None``).

        Also discovers command hooks in ``.bernstein/hooks/``.

        Args:
            workdir: Project root directory.  Defaults to ``Path.cwd()``.
        """
        self.discover_entry_points()

        root = workdir or Path.cwd()

        # Load command hooks from .bernstein/hooks
        hooks_dir = root / ".bernstein" / "hooks"
        if hooks_dir.is_dir():
            self.register(CommandHook(hooks_dir), name="command_hooks")

        config_path = root / "bernstein.yaml"
        if config_path.exists():
            try:
                import yaml  # type: ignore[import-untyped]

                # yaml.safe_load is untyped; work around via explicit annotation.
                loaded: object = yaml.safe_load(config_path.read_text())
                if not isinstance(loaded, dict):
                    return
                raw_plugins: object = loaded.get("plugins")  # type: ignore[union-attr]
                if isinstance(raw_plugins, list):
                    plugin_strs: list[str] = [str(item) for item in raw_plugins]  # type: ignore[var-annotated]
                    self.discover_config_plugins(plugin_strs)
            except Exception as exc:
                log.warning("Could not read plugins from bernstein.yaml: %s", exc)

    def register(self, plugin: object, name: str) -> None:
        """Register a plugin instance directly (useful in tests and scripts).

        Args:
            plugin: Any object with ``@hookimpl``-decorated methods.
            name: Unique name for this plugin instance.
        """
        self._pm.register(plugin, name=name)
        self._registered_names.append(name)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def registered_names(self) -> list[str]:
        """Names of all successfully registered plugins."""
        return list(self._registered_names)

    def plugin_hooks(self, plugin_name: str) -> list[str]:
        """Return names of hooks implemented by *plugin_name*.

        Args:
            plugin_name: Plugin name as returned by :attr:`registered_names`.

        Returns:
            Sorted list of hook names implemented by the plugin.
        """
        plugin = self._pm.get_plugin(plugin_name)
        if plugin is None:
            return []
        callers = self._pm.get_hookcallers(plugin)
        if callers is None:
            return []
        return sorted(hc.name for hc in callers)

    # ------------------------------------------------------------------
    # Fire methods
    # ------------------------------------------------------------------

    def _safe_call(self, hook_name: str, **kwargs: Any) -> None:
        """Invoke a hook, swallowing all exceptions from individual plugins.

        If the workspace is not trusted, all hooks are silently skipped with
        a warning log (T456).

        If the hook is marked as ``background=True`` in its specification,
        it will be scheduled for asynchronous execution in a thread pool.

        Args:
            hook_name: Name of the hook attribute on ``self._pm.hook``.
            **kwargs: Arguments forwarded to the hook.
        """
        if not self._check_workspace_trust():
            return

        try:
            hook_caller = getattr(self._pm.hook, hook_name)
            spec = getattr(hook_caller, "spec", None)
            is_background = False
            if spec and hasattr(spec.function, "bernstein_background"):
                is_background = bool(spec.function.bernstein_background)

            if is_background:
                log.debug("Scheduling background hook %r", hook_name)
                self._executor.submit(self._invoke_hook, hook_name, hook_caller, True, **kwargs)
            else:
                self._invoke_hook(hook_name, hook_caller, False, **kwargs)
        except HookBlockingError:
            # Re-raise blocking errors so they propagate to the orchestrator.
            raise
        except Exception as exc:
            log.warning("Plugin manager failed to dispatch hook %r: %s", hook_name, exc)

    def _check_workspace_trust(self) -> bool:
        """Check whether the workspace is trusted for hook execution (T456).

        Returns True if hooks are allowed to run, False if they should be
        skipped because trust has not been granted.

        Returns:
            True when hooks are allowed, False when gated.
        """
        if self._workdir is None or self._workdir == Path.cwd():
            return True
        if not is_workspace_trusted(self._workdir):
            log.warning(
                "Hook execution gated: workspace is not trusted (%s). Run the trust command to enable hook execution.",
                self._workdir,
            )
            return False
        return True

    def _invoke_hook(self, name: str, hook_caller: Any, is_background: bool, **kwargs: Any) -> None:
        """Actually execute the hook and log timing + outcome."""
        start = time.monotonic()
        outcome = "success"
        try:
            if is_background:
                log.debug("Starting background hook %r", name)
            hook_caller(**kwargs)
        except HookBlockingError:
            outcome = "blocking_error"
            raise
        except Exception as exc:
            outcome = "exception"
            log.warning("Plugin hook %r raised an exception: %s", name, exc)
        finally:
            duration = time.monotonic() - start
            tag = "background" if is_background else "foreground"
            if duration >= SLOW_HOOK_THRESHOLD:
                log.warning(
                    "Slow hook %s (%s): %.2fs (threshold=%.1fs)",
                    name,
                    tag,
                    duration,
                    SLOW_HOOK_THRESHOLD,
                )
            log.debug(
                "Hook %s (%s): outcome=%s duration=%.3fs",
                name,
                tag,
                outcome,
                duration,
            )
            if is_background:
                log.debug("Finished background hook %r", name)


def get_plugin_manager(workdir: Path | None = None, reload: bool = False) -> PluginManager:
    """Return the global :class:`PluginManager` instance.

    Args:
        workdir: Project root for loading local plugins.
        reload: If True, discard any existing manager and create a new one.

    Returns:
        The (possibly freshly constructed) :class:`PluginManager`.
    """
    global _manager
    if _manager is None or reload:
        _manager = PluginManager(workdir=workdir)
        _manager.load_from_workdir(workdir)
    return _manager
