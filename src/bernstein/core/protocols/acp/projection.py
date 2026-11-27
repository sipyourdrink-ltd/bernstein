"""Deterministic projection of :class:`EventJournal` rows onto ACP v1 ``session/update`` payloads.

Builds on :func:`bernstein.core.replay.thread_projection.project_journal` so live
``session/prompt`` streaming and ``session/load`` replay produce identical,
hash-anchored ACP v1 ``session/update`` notifications:

* ``plan`` (``AgentPlanUpdate``)
* ``tool_call`` (``ToolCallStart``) and ``tool_call_update`` (``ToolCallProgress``)
* ``user_message_chunk``, ``agent_thought_chunk``, ``agent_message_chunk``
* ``usage_update`` (``UsageUpdate``)
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, cast

from bernstein.core.replay.thread_projection import project_journal

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

DEFAULT_CONTEXT_WINDOW_TOKENS: Final[int] = 200_000
MAX_SESSION_LOAD_UPDATES: Final[int] = 2_000
MAX_SESSION_LOAD_BYTES: Final[int] = 16 * 1024 * 1024

VALID_ACP_TOOL_KINDS: Final[frozenset[str]] = frozenset(
    {
        "read",
        "edit",
        "delete",
        "move",
        "search",
        "execute",
        "think",
        "fetch",
        "switch_mode",
        "other",
    }
)


def infer_tool_kind(tool_name: str) -> str:
    """Map a Bernstein tool or step name onto one of the 10 ACP v1 ``ToolKind`` literals."""
    lower = tool_name.lower()
    if lower in VALID_ACP_TOOL_KINDS:
        return lower
    if any(tok in lower for tok in ("read", "view", "cat", "inspect")):
        return "read"
    if any(tok in lower for tok in ("edit", "write", "patch", "replace", "modify", "fix")):
        return "edit"
    if any(tok in lower for tok in ("delete", "remove", "unlink")):
        return "delete"
    if any(tok in lower for tok in ("move", "rename")):
        return "move"
    if any(tok in lower for tok in ("search", "grep", "glob", "find")):
        return "search"
    if any(tok in lower for tok in ("exec", "run", "bash", "shell", "cmd", "pytest", "test")):
        return "execute"
    if any(tok in lower for tok in ("fetch", "http", "curl", "web")):
        return "fetch"
    if any(tok in lower for tok in ("think", "reason", "plan")):
        return "think"
    return "other"


def _normalize_priority(raw: Any) -> str:
    if isinstance(raw, str):
        low = raw.lower()
        if low in {"high", "medium", "low"}:
            return low
    if isinstance(raw, int) and not isinstance(raw, bool):
        if raw <= 1:
            return "high"
        if raw <= 3:
            return "medium"
        return "low"
    return "medium"


def _normalize_plan_status(raw: Any) -> str:
    if isinstance(raw, str) and raw in {"pending", "in_progress", "completed"}:
        return raw
    return "pending"


def _journal_meta(index: int | None, event_hash: str) -> dict[str, Any] | None:
    meta: dict[str, Any] = {}
    if index is not None:
        meta["journalIndex"] = index
    if event_hash:
        meta["eventHash"] = event_hash
    return meta or None


def _safe_int(raw: Any, default: int = 0) -> int:
    if isinstance(raw, bool) or raw is None:
        return default
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw) if math.isfinite(raw) else default
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return default
        try:
            return int(stripped)
        except ValueError:
            return default
    return default


def _safe_float(raw: Any) -> float | None:
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        val = float(raw)
        return val if math.isfinite(val) else None
    if isinstance(raw, str):
        try:
            val = float(raw.strip())
        except ValueError:
            return None
        return val if math.isfinite(val) else None
    return None


@dataclass
class JournalACPProjector:
    """Stateful fold converting ordered :class:`EventJournal` rows into ACP ``session/update`` dicts."""

    context_window_tokens: int = DEFAULT_CONTEXT_WINDOW_TOKENS
    _plan_entries: dict[str, dict[str, str]] = field(default_factory=dict[str, dict[str, str]])
    _plan_order: list[str] = field(default_factory=list[str])
    _open_tool_calls: dict[str, str] = field(default_factory=dict[str, str])
    input_tokens: int = 0
    output_tokens: int = 0
    thought_tokens: int = 0

    def usage_summary(self) -> dict[str, int]:
        """Return cumulative turn token counts shaped like ``acp.schema.Usage``."""
        total = self.input_tokens + self.output_tokens + self.thought_tokens
        summary: dict[str, int] = {
            "totalTokens": total,
            "inputTokens": self.input_tokens,
            "outputTokens": self.output_tokens,
        }
        if self.thought_tokens > 0:
            summary["thoughtTokens"] = self.thought_tokens
        return summary

    def _emit_plan_snapshot(self, meta: dict[str, Any] | None) -> dict[str, Any]:
        upd: dict[str, Any] = {
            "sessionUpdate": "plan",
            "entries": [dict(self._plan_entries[tid]) for tid in self._plan_order if tid in self._plan_entries],
        }
        if meta is not None:
            upd["_meta"] = meta
        return upd

    def _upsert_plan_entry(
        self,
        task_id: str,
        *,
        content: str | None = None,
        priority: Any = None,
        status: str | None = None,
    ) -> None:
        existing = self._plan_entries.get(task_id)
        if existing is None:
            self._plan_entries[task_id] = {
                "content": content or task_id,
                "priority": _normalize_priority(priority),
                "status": status or "pending",
            }
            self._plan_order.append(task_id)
            return
        if content:
            existing["content"] = content
        if priority is not None:
            existing["priority"] = _normalize_priority(priority)
        if status is not None:
            existing["status"] = status

    def feed_event(
        self,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        journal_index: int | None = None,
        event_hash: str = "",
        include_user_messages: bool = False,
    ) -> list[dict[str, Any]]:
        """Project one journal event into zero or more ACP ``update`` dicts."""
        updates: list[dict[str, Any]] = []
        meta = _journal_meta(journal_index, event_hash)

        if event_type in {"user_prompt", "acp_user_message"}:
            if include_user_messages:
                text = str(payload.get("text") or payload.get("prompt") or "")
                if text:
                    upd: dict[str, Any] = {
                        "sessionUpdate": "user_message_chunk",
                        "content": {"type": "text", "text": text},
                    }
                    if meta is not None:
                        upd["_meta"] = meta
                    updates.append(upd)
            return updates

        if event_type in {"plan.graph.full", "plan.graph", "acp_plan_updated"}:
            tasks_raw = payload.get("tasks") or payload.get("entries")
            if isinstance(tasks_raw, list):
                self._plan_entries.clear()
                self._plan_order.clear()
                for idx, raw_item in enumerate(cast("list[Any]", tasks_raw)):
                    if not isinstance(raw_item, dict):
                        continue
                    item = cast("dict[str, Any]", raw_item)
                    tid = str(item.get("id") or item.get("task_id") or f"step-{idx + 1}")
                    title = str(item.get("title") or item.get("content") or tid)
                    self._upsert_plan_entry(
                        tid,
                        content=title,
                        priority=item.get("priority", "medium"),
                        status=_normalize_plan_status(item.get("status")),
                    )
                if self._plan_order:
                    updates.append(self._emit_plan_snapshot(meta))
            return updates

        if event_type == "task_claimed":
            tid = str(payload.get("task_id") or "")
            title = str(payload.get("title") or tid or "Execute workspace task")
            if tid:
                self._upsert_plan_entry(tid, content=title, status="in_progress")
                updates.append(self._emit_plan_snapshot(meta))
            tc_id = str(payload.get("tool_call_id") or (f"call-{tid}" if tid else "call-task"))
            tool_name = str(payload.get("tool") or "workspace_task")
            tc_upd: dict[str, Any] = {
                "sessionUpdate": "tool_call",
                "toolCallId": tc_id,
                "title": title,
                "name": tool_name,
                "kind": infer_tool_kind(tool_name),
                "status": "in_progress",
            }
            args_raw = payload.get("args")
            if isinstance(args_raw, dict):
                args_map = cast("dict[str, Any]", args_raw)
                tc_upd["rawInput"] = args_map
                loc_path = args_map.get("path") or args_map.get("file_path")
                if isinstance(loc_path, str) and loc_path:
                    tc_upd["locations"] = [{"path": loc_path}]
            if meta is not None:
                tc_upd["_meta"] = meta
            self._open_tool_calls[tid or tc_id] = tc_id
            updates.append(tc_upd)
            return updates

        if event_type in {"tool_call", "mcp_call"}:
            tc_id = str(payload.get("tool_call_id") or payload.get("call_id") or f"call-{journal_index or 0}")
            tool_name = str(payload.get("tool") or payload.get("tool_name") or "tool")
            title = str(payload.get("title") or f"{tool_name}")
            status_raw = payload.get("status")
            status = (
                status_raw
                if isinstance(status_raw, str) and status_raw in {"pending", "in_progress"}
                else "in_progress"
            )
            tc_start: dict[str, Any] = {
                "sessionUpdate": "tool_call",
                "toolCallId": tc_id,
                "title": title,
                "name": tool_name,
                "kind": infer_tool_kind(str(payload.get("kind") or tool_name)),
                "status": status,
            }
            path_val = payload.get("path") or payload.get("file_path")
            args_raw = payload.get("args")
            if isinstance(args_raw, dict):
                args_dict = cast("dict[str, Any]", args_raw)
                tc_start["rawInput"] = args_dict
                if not path_val:
                    path_val = args_dict.get("path") or args_dict.get("file_path")
            if isinstance(path_val, str) and path_val:
                tc_start["locations"] = [{"path": path_val}]
            if meta is not None:
                tc_start["_meta"] = meta
            self._open_tool_calls[tc_id] = tc_id
            updates.append(tc_start)
            return updates

        if event_type in {"task_diff_captured", "tool_call_completed", "tool_result", "artifact_produced"}:
            tid = str(payload.get("task_id") or "")
            tc_id = str(
                payload.get("tool_call_id")
                or self._open_tool_calls.get(tid)
                or (f"call-{tid}" if tid else f"call-{journal_index or 0}")
            )
            status_val = str(payload.get("status") or "completed")
            if status_val not in {"pending", "in_progress", "completed", "failed"}:
                status_val = "completed"
            prog: dict[str, Any] = {
                "sessionUpdate": "tool_call_update",
                "toolCallId": tc_id,
                "status": status_val,
            }
            path_val = payload.get("path") or payload.get("file_path")
            new_text = payload.get("new_text")
            old_text = payload.get("old_text")
            if isinstance(path_val, str) and path_val:
                prog["locations"] = [{"path": path_val}]
                if isinstance(new_text, str):
                    diff_block: dict[str, Any] = {
                        "type": "diff",
                        "path": path_val,
                        "newText": new_text,
                    }
                    if isinstance(old_text, str):
                        diff_block["oldText"] = old_text
                    prog["content"] = [diff_block]
            output_text = payload.get("output")
            if "content" not in prog and isinstance(output_text, str) and output_text:
                prog["content"] = [{"type": "content", "content": {"type": "text", "text": output_text}}]
            if meta is not None:
                prog["_meta"] = meta
            updates.append(prog)
            return updates

        if event_type in {"task_completed", "task_merged"}:
            tid = str(payload.get("task_id") or "")
            if tid and tid in self._plan_entries:
                self._plan_entries[tid]["status"] = "completed"
                updates.append(self._emit_plan_snapshot(meta))
            tc_id = self._open_tool_calls.pop(tid, "")
            if tc_id:
                done_upd: dict[str, Any] = {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": tc_id,
                    "status": "completed",
                }
                summary_txt = payload.get("summary")
                if isinstance(summary_txt, str) and summary_txt:
                    done_upd["content"] = [{"type": "content", "content": {"type": "text", "text": summary_txt}}]
                if meta is not None:
                    done_upd["_meta"] = meta
                updates.append(done_upd)
            return updates

        if event_type in {"task_verification_failed", "task_failed", "task_cancelled"}:
            tid = str(payload.get("task_id") or "")
            tc_id = self._open_tool_calls.pop(tid, "") or (f"call-{tid}" if tid else "")
            if tc_id:
                fail_upd: dict[str, Any] = {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": tc_id,
                    "status": "failed",
                }
                reason_txt = payload.get("reason")
                if isinstance(reason_txt, str) and reason_txt:
                    fail_upd["content"] = [{"type": "content", "content": {"type": "text", "text": reason_txt}}]
                if meta is not None:
                    fail_upd["_meta"] = meta
                updates.append(fail_upd)
            return updates

        if event_type in {"agent_thought", "thinking", "reasoning_chunk"}:
            text = str(payload.get("text") or "")
            if text:
                thought_upd: dict[str, Any] = {
                    "sessionUpdate": "agent_thought_chunk",
                    "content": {"type": "text", "text": text},
                }
                if meta is not None:
                    thought_upd["_meta"] = meta
                updates.append(thought_upd)
            return updates

        if event_type in {"agent_message", "assistant_chunk", "run_summary", "activity_result"}:
            text = str(payload.get("text") or payload.get("summary") or payload.get("reason_code") or "")
            if text:
                msg_upd: dict[str, Any] = {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": text},
                }
                if meta is not None:
                    msg_upd["_meta"] = meta
                updates.append(msg_upd)
            return updates

        if event_type in {"usage_update", "token_usage", "cost_recorded", "run_completed"}:
            in_tok = _safe_int(payload.get("input_tokens"), 0)
            out_tok = _safe_int(payload.get("output_tokens"), 0)
            th_tok = _safe_int(payload.get("thought_tokens"), 0)
            self.input_tokens += max(0, in_tok)
            self.output_tokens += max(0, out_tok)
            self.thought_tokens += max(0, th_tok)

            raw_used = payload.get("used")
            if raw_used is None:
                raw_used = payload.get("total_tokens")
            has_usage_signal = raw_used is not None or any(
                k in payload for k in ("input_tokens", "output_tokens", "thought_tokens", "used", "total_tokens")
            )
            if has_usage_signal:
                computed_total = self.input_tokens + self.output_tokens + self.thought_tokens
                used_val = _safe_int(raw_used, computed_total) if raw_used is not None else computed_total
                raw_size = payload.get("size") if payload.get("size") is not None else payload.get("context_window")
                size_val = _safe_int(raw_size, self.context_window_tokens)
                if size_val <= 0:
                    size_val = self.context_window_tokens
                usage_upd: dict[str, Any] = {
                    "sessionUpdate": "usage_update",
                    "used": max(0, used_val),
                    "size": max(1, size_val),
                }
                cost_val = _safe_float(payload.get("cost_usd"))
                if cost_val is not None:
                    usage_upd["cost"] = {
                        "amount": cost_val,
                        "currency": str(payload.get("currency") or "USD"),
                    }
                if meta is not None:
                    usage_upd["_meta"] = meta
                updates.append(usage_upd)
            return updates

        return updates

    def feed_raw_entry(
        self,
        entry: Mapping[str, Any],
        *,
        include_user_messages: bool = False,
    ) -> list[dict[str, Any]]:
        """Project a raw ``EventJournal`` observer entry dict."""
        event_type = str(entry.get("event") or "")
        raw_idx = entry.get("index")
        idx = int(raw_idx) if isinstance(raw_idx, int) and not isinstance(raw_idx, bool) else None
        event_hash = str(entry.get("event_hash") or "")
        return self.feed_event(
            event_type,
            entry,
            journal_index=idx,
            event_hash=event_hash,
            include_user_messages=include_user_messages,
        )


def project_journal_to_acp_updates(
    journal_path: Path,
    *,
    include_user_messages: bool = True,
    after_index: int | None = None,
    max_updates: int | None = None,
    max_bytes: int | None = None,
) -> list[dict[str, Any]]:
    """Deterministically project a ``journal.jsonl`` file into ACP ``session/update`` dicts.

    When *max_updates* or *max_bytes* is supplied (e.g. during ``session/load``),
    returns at most the newest updates fitting within both caps in chronological order.
    """
    projector = JournalACPProjector()
    out: list[dict[str, Any]] = []
    for step in project_journal(journal_path, after_index=after_index):
        out.extend(
            projector.feed_event(
                step.journal_event,
                step.payload,
                journal_index=step.journal_index,
                event_hash=step.event_hash,
                include_user_messages=include_user_messages,
            )
        )
    if max_updates is not None and max_updates >= 0 and len(out) > max_updates:
        out = out[-max_updates:] if max_updates > 0 else []
    if max_bytes is not None and max_bytes >= 0 and out:
        kept_reversed: list[dict[str, Any]] = []
        used_bytes = 0
        for item in reversed(out):
            encoded_len = len(json.dumps(item, separators=(",", ":")).encode("utf-8"))
            if used_bytes + encoded_len > max_bytes:
                break
            kept_reversed.append(item)
            used_bytes += encoded_len
        out = list(reversed(kept_reversed))
    return out


__all__ = [
    "DEFAULT_CONTEXT_WINDOW_TOKENS",
    "MAX_SESSION_LOAD_BYTES",
    "MAX_SESSION_LOAD_UPDATES",
    "VALID_ACP_TOOL_KINDS",
    "JournalACPProjector",
    "infer_tool_kind",
    "project_journal_to_acp_updates",
]
