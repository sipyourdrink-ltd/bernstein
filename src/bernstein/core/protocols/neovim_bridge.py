"""Neovim integration bridge for Bernstein orchestrator.

Provides a Python-side bridge that generates Lua plugin code, handles
commands dispatched from the Neovim plugin, and formats data for
display in Neovim splits and statuslines.

This module does NOT depend on any Neovim Python bindings -- the Lua
plugin is self-contained and communicates with the Bernstein task
server over HTTP.

Usage::

    from bernstein.core.protocols.neovim_bridge import NeovimBridge

    bridge = NeovimBridge(server_url="http://127.0.0.1:8052")
    cmds = bridge.get_available_commands()
    result = bridge.handle_command("status", {})
"""

from __future__ import annotations

import textwrap
import time
from dataclasses import dataclass, field
from typing import Any

# Default Bernstein task-server URL.
_DEFAULT_SERVER_URL = "http://127.0.0.1:8052"

# Neovim commands exposed by the plugin.
_COMMAND_DEFINITIONS: list[dict[str, str]] = [
    {
        "name": "BernsteinRun",
        "description": "Start a Bernstein orchestration run",
        "args_spec": "[plan_file:string]",
    },
    {
        "name": "BernsteinStatus",
        "description": "Show orchestrator status in a split pane",
        "args_spec": "",
    },
    {
        "name": "BernsteinPlan",
        "description": "Open or reload the current plan file",
        "args_spec": "[plan_file:string]",
    },
    {
        "name": "BernsteinStop",
        "description": "Stop the running orchestration gracefully",
        "args_spec": "",
    },
]


@dataclass(frozen=True)
class NeovimCommand:
    """Descriptor for a command the Neovim plugin exposes.

    Attributes:
        name: Vim command name (e.g. ``BernsteinRun``).
        description: One-line human-readable description.
        args_spec: Argument specification string (empty if none).
    """

    name: str
    description: str
    args_spec: str


@dataclass(frozen=True)
class NeovimEvent:
    """An event emitted by the Neovim bridge.

    Attributes:
        event_type: Type tag (e.g. ``command``, ``status_refresh``).
        data: Arbitrary payload dict.
        timestamp: Unix epoch seconds when the event was created.
    """

    event_type: str
    data: dict[str, Any] = field(default_factory=dict)  # type: ignore[reportUnknownVariableType]
    timestamp: float = field(default_factory=time.time)


class NeovimBridge:
    """Python-side bridge for the Neovim Bernstein plugin.

    Dispatches commands, formats output for splits/statusline, and
    produces inline diff annotations.

    Args:
        server_url: Base URL of the Bernstein task server.
    """

    def __init__(self, server_url: str = _DEFAULT_SERVER_URL) -> None:
        self._server_url = server_url
        self._events: list[NeovimEvent] = []

    # -- Commands -------------------------------------------------------------

    def get_available_commands(self) -> list[NeovimCommand]:
        """Return the list of Neovim commands the plugin provides."""
        return [
            NeovimCommand(
                name=d["name"],
                description=d["description"],
                args_spec=d["args_spec"],
            )
            for d in _COMMAND_DEFINITIONS
        ]

    def handle_command(
        self,
        name: str,
        args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Dispatch a command by name and return a response dict.

        Args:
            name: Command name (e.g. ``BernsteinRun``).
            args: Optional argument mapping.

        Returns:
            A dict with at least ``"ok"`` (bool) and ``"message"`` keys.
        """
        args = args or {}
        handler = self._command_handlers().get(name)
        if handler is None:
            return {"ok": False, "message": f"Unknown command: {name}"}
        return handler(args)

    # -- Formatting -----------------------------------------------------------

    def format_status_line(self, status: dict[str, Any]) -> str:
        """Format orchestrator status for the Neovim statusline.

        The output is a compact, single-line string suitable for
        ``statusline`` or ``winbar`` integration.

        Args:
            status: Status dict (as returned by ``GET /status``).

        Returns:
            A short status string like ``[BST] 3/5 tasks | 2 agents``.
        """
        total = int(status.get("total_tasks", 0))
        completed = int(status.get("completed_tasks", 0))
        agents = int(status.get("active_agents", 0))
        state = str(status.get("state", "unknown"))

        return f"[BST:{state}] {completed}/{total} tasks | {agents} agents"

    def format_split_output(self, agent_output: str) -> list[str]:
        """Format agent output for display in a Neovim split pane.

        Wraps long lines and prefixes each block with a separator.

        Args:
            agent_output: Raw text output from an agent.

        Returns:
            List of lines ready for ``nvim_buf_set_lines``.
        """
        lines: list[str] = []
        lines.append("--- Bernstein Agent Output ---")
        lines.append("")
        for raw_line in agent_output.splitlines():
            if len(raw_line) > 120:
                wrapped = textwrap.wrap(raw_line, width=120)
                lines.extend(wrapped)
            else:
                lines.append(raw_line)
        lines.append("")
        lines.append("--- End ---")
        return lines

    def get_diff_annotations(
        self,
        changed_files: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Return inline annotation data for changed files.

        Each annotation describes a change that can be shown as a virtual
        text or sign in the Neovim gutter.

        Args:
            changed_files: List of dicts with ``file``, ``line``,
                ``change_type``, and optional ``summary`` keys.

        Returns:
            List of annotation dicts with ``file``, ``line``,
            ``sign``, and ``text`` keys.
        """
        sign_map: dict[str, str] = {
            "added": "+",
            "modified": "~",
            "deleted": "-",
        }
        annotations: list[dict[str, Any]] = []
        for entry in changed_files:
            change_type = str(entry.get("change_type", "modified"))
            sign = sign_map.get(change_type, "?")
            text = str(entry.get("summary", change_type))
            annotations.append(
                {
                    "file": str(entry.get("file", "")),
                    "line": int(entry.get("line", 0)),
                    "sign": sign,
                    "text": f"[BST] {text}",
                }
            )
        return annotations

    # -- Internal command handlers -------------------------------------------

    def _command_handlers(
        self,
    ) -> dict[str, Any]:
        """Map command names to handler callables."""
        return {
            "BernsteinRun": self._handle_run,
            "BernsteinStatus": self._handle_status,
            "BernsteinPlan": self._handle_plan,
            "BernsteinStop": self._handle_stop,
        }

    def _handle_run(self, args: dict[str, Any]) -> dict[str, Any]:
        plan_file = str(args.get("plan_file", ""))
        event = NeovimEvent(
            event_type="command",
            data={"command": "BernsteinRun", "plan_file": plan_file},
        )
        self._events.append(event)
        msg = f"Run started (plan: {plan_file})" if plan_file else "Run started"
        return {
            "ok": True,
            "message": msg,
            "server_url": self._server_url,
        }

    def _handle_status(self, args: dict[str, Any]) -> dict[str, Any]:
        event = NeovimEvent(
            event_type="command",
            data={"command": "BernsteinStatus"},
        )
        self._events.append(event)
        return {
            "ok": True,
            "message": "Fetching status...",
            "server_url": self._server_url,
        }

    def _handle_plan(self, args: dict[str, Any]) -> dict[str, Any]:
        plan_file = str(args.get("plan_file", ""))
        event = NeovimEvent(
            event_type="command",
            data={"command": "BernsteinPlan", "plan_file": plan_file},
        )
        self._events.append(event)
        msg = f"Plan loaded: {plan_file}" if plan_file else "No plan file specified"
        return {
            "ok": True,
            "message": msg,
        }

    def _handle_stop(self, args: dict[str, Any]) -> dict[str, Any]:
        event = NeovimEvent(
            event_type="command",
            data={"command": "BernsteinStop"},
        )
        self._events.append(event)
        return {
            "ok": True,
            "message": "Stop signal sent",
            "server_url": self._server_url,
        }


# ---------------------------------------------------------------------------
# Plugin generation helpers
# ---------------------------------------------------------------------------


def generate_plugin_lua(server_url: str = _DEFAULT_SERVER_URL) -> str:
    """Generate the Lua source for the Neovim Bernstein plugin.

    The generated code is self-contained: it uses ``vim.fn.system`` /
    ``curl`` to communicate with the Bernstein task server and does
    NOT require any Python or Neovim RPC bindings.

    Args:
        server_url: Base URL of the Bernstein task server.

    Returns:
        Complete Lua source as a string.
    """
    return textwrap.dedent(f"""\
        -- Bernstein Neovim integration
        -- Commands: :BernsteinRun, :BernsteinStatus, :BernsteinPlan, :BernsteinStop
        --
        -- Generated by bernstein.core.protocols.neovim_bridge
        -- Do not edit manually; regenerate with generate_plugin_lua().

        local M = {{}}

        M.config = {{
          server_url = "{server_url}",
          split_direction = "botright",
          split_height = 15,
          statusline = true,
          auto_refresh = true,
          refresh_interval_ms = 5000,
        }}

        -- Internal state -------------------------------------------------------

        local _buf = nil
        local _win = nil
        local _timer = nil

        -- Helpers --------------------------------------------------------------

        local function request(method, path, body)
          local url = M.config.server_url .. path
          local cmd
          if method == "GET" then
            cmd = string.format("curl -s -X GET %s", vim.fn.shellescape(url))
          else
            local json = body or "{{}}"
            cmd = string.format(
              "curl -s -X %s -H 'Content-Type: application/json' -d %s %s",
              method,
              vim.fn.shellescape(json),
              vim.fn.shellescape(url)
            )
          end
          local result = vim.fn.system(cmd)
          local ok, decoded = pcall(vim.fn.json_decode, result)
          if ok then
            return decoded
          end
          return {{ error = result }}
        end

        local function open_split()
          if _win and vim.api.nvim_win_is_valid(_win) then
            vim.api.nvim_set_current_win(_win)
            return
          end
          vim.cmd(M.config.split_direction .. " " .. M.config.split_height .. "split")
          _buf = vim.api.nvim_create_buf(false, true)
          _win = vim.api.nvim_get_current_win()
          vim.api.nvim_win_set_buf(_win, _buf)
          vim.api.nvim_buf_set_option(_buf, "buftype", "nofile")
          vim.api.nvim_buf_set_option(_buf, "filetype", "bernstein")
          vim.api.nvim_buf_set_option(_buf, "modifiable", false)
        end

        local function set_buf_lines(lines)
          if not _buf or not vim.api.nvim_buf_is_valid(_buf) then
            return
          end
          vim.api.nvim_buf_set_option(_buf, "modifiable", true)
          vim.api.nvim_buf_set_lines(_buf, 0, -1, false, lines)
          vim.api.nvim_buf_set_option(_buf, "modifiable", false)
        end

        local function format_status(data)
          local lines = {{}}
          table.insert(lines, "--- Bernstein Status ---")
          table.insert(lines, "")
          if data.error then
            table.insert(lines, "Error: " .. tostring(data.error))
          else
            for k, v in pairs(data) do
              table.insert(lines, string.format("  %s: %s", k, tostring(v)))
            end
          end
          table.insert(lines, "")
          table.insert(lines, "--- End ---")
          return lines
        end

        -- Commands -------------------------------------------------------------

        function M.run(opts)
          local plan = (opts and opts.args ~= "") and opts.args or nil
          local body = plan and vim.fn.json_encode({{ plan_file = plan }}) or "{{}}"
          local resp = request("POST", "/tasks", body)
          if resp and not resp.error then
            vim.notify("[Bernstein] Run started", vim.log.levels.INFO)
          else
            vim.notify("[Bernstein] Run failed: " .. tostring(resp and resp.error or "unknown"), vim.log.levels.ERROR)
          end
        end

        function M.status()
          open_split()
          local data = request("GET", "/status")
          local lines = format_status(data or {{}})
          set_buf_lines(lines)
        end

        function M.plan(opts)
          local file = (opts and opts.args ~= "") and opts.args or nil
          if not file then
            vim.notify("[Bernstein] Usage: :BernsteinPlan <file>", vim.log.levels.WARN)
            return
          end
          vim.cmd("edit " .. file)
          vim.notify("[Bernstein] Plan loaded: " .. file, vim.log.levels.INFO)
        end

        function M.stop()
          vim.notify("[Bernstein] Sending stop signal...", vim.log.levels.INFO)
          request("POST", "/stop")
          vim.notify("[Bernstein] Stop signal sent", vim.log.levels.INFO)
        end

        -- Statusline -----------------------------------------------------------

        function M.statusline()
          local ok, data = pcall(request, "GET", "/status")
          if not ok or not data or data.error then
            return "[BST:offline]"
          end
          local total = data.total_tasks or 0
          local completed = data.completed_tasks or 0
          local agents = data.active_agents or 0
          local state = data.state or "unknown"
          return string.format("[BST:%s] %d/%d tasks | %d agents", state, completed, total, agents)
        end

        -- Auto-refresh ---------------------------------------------------------

        function M.start_auto_refresh()
          if _timer then
            return
          end
          _timer = vim.loop.new_timer()
          _timer:start(0, M.config.refresh_interval_ms, vim.schedule_wrap(function()
            if _win and vim.api.nvim_win_is_valid(_win) then
              local data = request("GET", "/status")
              local lines = format_status(data or {{}})
              set_buf_lines(lines)
            end
          end))
        end

        function M.stop_auto_refresh()
          if _timer then
            _timer:stop()
            _timer:close()
            _timer = nil
          end
        end

        -- Setup ----------------------------------------------------------------

        function M.setup(user_config)
          if user_config then
            M.config = vim.tbl_deep_extend("force", M.config, user_config)
          end

          vim.api.nvim_create_user_command("BernsteinRun", M.run, {{ nargs = "?" }})
          vim.api.nvim_create_user_command("BernsteinStatus", M.status, {{ nargs = 0 }})
          vim.api.nvim_create_user_command("BernsteinPlan", M.plan, {{ nargs = "?" }})
          vim.api.nvim_create_user_command("BernsteinStop", M.stop, {{ nargs = 0 }})
        end

        return M
    """)


def render_setup_guide() -> str:
    """Return a Markdown installation guide for the Neovim plugin.

    Returns:
        Markdown string with setup instructions.
    """
    return textwrap.dedent("""\
        # Bernstein Neovim Plugin

        ## Requirements

        - Neovim >= 0.9
        - `curl` on PATH (for HTTP requests to the task server)
        - A running Bernstein task server (`bernstein run`)

        ## Installation

        ### lazy.nvim

        ```lua
        {
          dir = "path/to/bernstein/integrations/neovim",
          config = function()
            require("bernstein").setup({
              server_url = "http://127.0.0.1:8052",
            })
          end,
        }
        ```

        ### Manual

        Copy `integrations/neovim/lua/bernstein/init.lua` into your
        Neovim runtime path:

        ```
        ~/.config/nvim/lua/bernstein/init.lua
        ```

        Then in your `init.lua`:

        ```lua
        require("bernstein").setup()
        ```

        ## Commands

        | Command             | Description                              |
        |---------------------|------------------------------------------|
        | `:BernsteinRun`     | Start an orchestration run               |
        | `:BernsteinStatus`  | Show status in a split pane              |
        | `:BernsteinPlan`    | Open or reload a plan file               |
        | `:BernsteinStop`    | Stop the running orchestration           |

        ## Statusline

        Add to your statusline or lualine config:

        ```lua
        require("bernstein").statusline()
        ```

        ## Configuration

        ```lua
        require("bernstein").setup({
          server_url = "http://127.0.0.1:8052",
          split_direction = "botright",   -- split placement
          split_height = 15,              -- split height in lines
          statusline = true,              -- enable statusline component
          auto_refresh = true,            -- auto-refresh status split
          refresh_interval_ms = 5000,     -- refresh interval
        })
        ```
    """)
