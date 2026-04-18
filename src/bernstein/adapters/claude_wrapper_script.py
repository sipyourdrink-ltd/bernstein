"""Inline wrapper script source + assembly for the Claude Code adapter.

Extracted from :mod:`bernstein.adapters.claude` in audit-142.  The wrapper
script is a short Python program that reads Claude Code's stream-json
NDJSON output, prints human-readable text to the adapter's log file,
records token usage to a sidecar, touches a heartbeat file on every
event, and writes a completion marker when the agent emits a ``result``
event.  Keeping it in its own module leaves the adapter shell free of
104 lines of embedded Python source.

The public entry point :func:`build_wrapper_script` preserves the exact
behaviour of the original ``ClaudeCodeAdapter._wrapper_script`` staticmethod
so golden-replay tests from audit-141 pass unchanged.
"""

from __future__ import annotations

# Prelude: shared across every wrapper invocation regardless of which
# sidecar/heartbeat/completion features are enabled.  The wrapper reads
# NDJSON one line at a time, extracts printable text for the adapter log,
# then optionally runs the feature-specific blocks below.
_WRAPPER_PRELUDE: str = (
    "import sys, json\n"
    "seen_text = set()\n"
    "for raw in sys.stdin:\n"
    "    raw = raw.strip()\n"
    "    if not raw:\n"
    "        continue\n"
    "    try:\n"
    "        msg = json.loads(raw)\n"
    "    except json.JSONDecodeError:\n"
    "        continue\n"
)

# Per-event assistant/result dispatch and RESULT log line.  Runs after
# the optional heartbeat block so heartbeats fire even when the message
# is a no-op assistant chunk.
_WRAPPER_DISPATCH: str = (
    "    t = msg.get('type', '')\n"
    "    if t == 'assistant':\n"
    "        for block in msg.get('message', {}).get('content', []):\n"
    "            if block.get('type') == 'text':\n"
    "                txt = block['text']\n"
    "                if txt not in seen_text:\n"
    "                    seen_text.add(txt)\n"
    "                    print(txt, flush=True)\n"
    "            elif block.get('type') == 'tool_use':\n"
    "                name = block.get('name', '?')\n"
    "                inp = str(block.get('input', ''))[:150]\n"
    "                print(f'[{name}] {inp}', flush=True)\n"
    "    elif t == 'result':\n"
    "        txt = msg.get('result', '')\n"
    "        if txt:\n"
    "            print(txt, flush=True)\n"
    "        # Extract structured result data for orchestrator\n"
    "        _subtype = msg.get('subtype', 'success')\n"
    "        _cost = msg.get('total_cost_usd', 0.0)\n"
    "        _turns = msg.get('num_turns', 0)\n"
    "        _dur = msg.get('duration_ms', 0)\n"
    "        print(f'[RESULT] subtype={_subtype} cost=${_cost:.4f}'"
    "              f' turns={_turns} duration={_dur}ms', flush=True)\n"
)


def _build_token_writer(tokens_path: str) -> str:
    """Return the token-sidecar writer block substituted for *tokens_path*."""
    if not tokens_path:
        return ""
    return (
        "        usage = msg.get('usage') or {}\n"
        "        if not usage:\n"
        "            usage = msg.get('message', {}).get('usage') or {}\n"
        "        inp_tok = int(usage.get('input_tokens', 0))\n"
        "        out_tok = int(usage.get('output_tokens', 0))\n"
        "        if inp_tok or out_tok:\n"
        "            import time as _t\n"
        f"            _rec = json.dumps({{'ts': _t.time(), 'in': inp_tok, 'out': out_tok}})\n"
        f"            try:\n"
        f"                with open({tokens_path!r}, 'a') as _tf:\n"
        f"                    _tf.write(_rec + '\\n')\n"
        f"            except OSError:\n"
        f"                pass\n"
    )


def _build_heartbeat_touch(heartbeat_path: str) -> str:
    """Return the heartbeat-touch block substituted for *heartbeat_path*.

    Heartbeat touches fire on every parsed JSON event so the orchestrator
    gets a real-time liveness signal instead of relying on log file mtime
    which may buffer.
    """
    if not heartbeat_path:
        return ""
    return (
        "    # Touch heartbeat file on every event\n"
        "    try:\n"
        "        _hb = {'timestamp': __import__('time').time(), 'phase': 'implementing',"
        " 'progress_pct': 0, 'current_file': '', 'message': 'working', 'status': 'working'}\n"
        f"        with open({heartbeat_path!r}, 'w') as _hf:\n"
        f"            _hf.write(__import__('json').dumps(_hb))\n"
        "    except OSError:\n"
        "        pass\n"
    )


def _build_completion_write(completion_path: str) -> str:
    """Return the completion-marker writer block for *completion_path*.

    Written when the agent emits a ``result`` event so the orchestrator
    can reap the slot immediately instead of waiting for the heartbeat
    to go stale (saves up to 300s per agent).
    """
    if not completion_path:
        return ""
    return (
        "        try:\n"
        "            import json as _json\n"
        "            _marker = _json.dumps({'result': txt or '', 'subtype': _subtype,"
        " 'cost_usd': _cost, 'turns': _turns, 'duration_ms': _dur})\n"
        f"            with open({completion_path!r}, 'w') as _cf:\n"
        f"                _cf.write(_marker)\n"
        "        except OSError:\n"
        "            pass\n"
    )


def build_wrapper_script(
    session_id: str = "",
    tokens_path: str = "",
    heartbeat_path: str = "",
    completion_path: str = "",
) -> str:
    """Return the stream-json → human-readable log converter script.

    Parses Claude Code's NDJSON stream, extracts human-readable text for
    the log file, writes token usage to a sidecar, touches a heartbeat
    file on every event so the orchestrator knows the agent is alive, and
    writes a completion marker when a ``result`` event is received so the
    orchestrator can reap the agent immediately instead of waiting for the
    heartbeat to go stale.

    Args:
        session_id: Agent session ID, accepted for API parity with the
            original staticmethod.  Reserved for future sidecar formats
            that embed the session ID in records.
        tokens_path: Absolute path to the ``.tokens`` sidecar file.
        heartbeat_path: Absolute path to the heartbeat file (touched on each event).
        completion_path: Absolute path to the completion marker file.  Written
            when a ``result`` event is parsed, signalling the orchestrator that
            the agent finished its work and can be reaped immediately.
    """
    # ``session_id`` is accepted but not referenced in the emitted source
    # today.  Kept in the signature so the adapter call-site and tests
    # that rely on the argument order do not change.
    _ = session_id

    token_writer = _build_token_writer(tokens_path)
    heartbeat_touch = _build_heartbeat_touch(heartbeat_path)
    completion_write = _build_completion_write(completion_path)

    return _WRAPPER_PRELUDE + heartbeat_touch + _WRAPPER_DISPATCH + completion_write + token_writer


__all__ = ["build_wrapper_script"]
