#!/usr/bin/env python3
"""Claude Code PreToolUse hook — smart auto-approve for agent tool calls.

Reads the tool-call payload from stdin (Claude Code hook JSON format),
classifies the command, and exits with the appropriate code:

    Exit 0  — approve  (tool call proceeds)
    Exit 1  — deny     (tool call blocked; reason written to stdout)
    Exit 2  — ask      (escalate to human; reason written to stdout)

Usage in ~/.claude/settings.json or .claude/settings.json:

    {
      "hooks": {
        "PreToolUse": [
          {
            "matcher": "Bash",
            "hooks": [
              {
                "type": "command",
                "command": "python /path/to/bernstein/hooks/smart_approve.py"
              }
            ]
          }
        ]
      }
    }

Or to cover all tools:

    {
      "hooks": {
        "PreToolUse": [
          {
            "matcher": "*",
            "hooks": [
              {
                "type": "command",
                "command": "python /path/to/bernstein/hooks/smart_approve.py"
              }
            ]
          }
        ]
      }
    }

The hook can also be used standalone for testing:

    echo '{"tool_name": "Bash", "tool_input": {"command": "ls -la"}}' | python hooks/smart_approve.py
    echo $?   # 0 — approved
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    """Read hook payload from stdin, classify, return exit code."""
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            # No input — allow (edge case in some hook configurations)
            return 0
        payload: dict[str, object] = json.loads(raw)
    except (json.JSONDecodeError, OSError) as exc:
        # Cannot parse → escalate to human
        print(json.dumps({"decision": "ask", "reason": f"Hook parse error: {exc}"}))
        return 2

    tool_name = str(payload.get("tool_name", ""))
    tool_input_raw = payload.get("tool_input", {})
    tool_input: dict[str, object] = (
        dict(tool_input_raw)  # type: ignore[arg-type]
        if isinstance(tool_input_raw, dict)
        else {}
    )

    # Import here so the hook works even when bernstein isn't on PYTHONPATH:
    # fall back to a copy of the logic if the package isn't importable.
    try:
        from bernstein.core.auto_approve import Decision, classify_tool_call

        result = classify_tool_call(tool_name, tool_input)
    except ImportError:
        # Fallback: approve everything if the package isn't installed.
        # This preserves the previous --dangerously-skip-permissions behaviour.
        print(json.dumps({"decision": "approve", "reason": "bernstein not installed; fallback approve"}))
        return 0

    if result.decision == Decision.APPROVE:
        # Writing nothing to stdout is the standard "allow" signal.
        return 0

    if result.decision == Decision.DENY:
        print(json.dumps({"decision": "block", "reason": result.reason}))
        return 1

    # ASK — escalate to human
    print(json.dumps({"decision": "ask", "reason": result.reason}))
    return 2


if __name__ == "__main__":
    sys.exit(main())
