from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping

    from bernstein.core.persistence.action_cache import ActionRecord


class WorkerLoopDetector:
    """Detects repeating action cycles from a worker to prevent infinite loops.

    Observes canonical ActionRecords and fires when a threshold of identical
    actions or identical alternating pairs is reached.
    """

    def __init__(self, threshold: int = 3, max_interventions: int = 3) -> None:
        self.threshold = threshold
        self.max_interventions = max_interventions
        self._interventions_used = 0
        self._recent_actions: list[ActionRecord] = []

    def observe(self, record: ActionRecord) -> Literal["identical", "alternating"] | None:
        """Observe a canonical action and return the loop type if detected.

        Returns:
            "identical" if threshold identical consecutive actions are detected.
            "alternating" if threshold identical alternating pairs are detected.
            None otherwise (no loop, or budget exhausted).
        """
        self._recent_actions.append(record)

        # We need at least threshold actions to detect a loop
        if len(self._recent_actions) < self.threshold:
            return None

        # Check for N identical actions
        recent_n = self._recent_actions[-self.threshold :]
        if all(_is_identical(recent_n[0], r) for r in recent_n) and (self._interventions_used < self.max_interventions):
            self._interventions_used += 1
            self._recent_actions.clear()
            return "identical"

        # Check for N alternating pairs (e.g. A, B, A, B, A, B)
        if len(self._recent_actions) >= self.threshold * 2:
            recent_2n = self._recent_actions[-(self.threshold * 2) :]
            a_action = recent_2n[0]
            b_action = recent_2n[1]

            if not _is_identical(a_action, b_action):
                is_alternating = True
                for i in range(self.threshold):
                    if not _is_identical(a_action, recent_2n[i * 2]):
                        is_alternating = False
                        break
                    if not _is_identical(b_action, recent_2n[i * 2 + 1]):
                        is_alternating = False
                        break

                if is_alternating and self._interventions_used < self.max_interventions:
                    self._interventions_used += 1
                    self._recent_actions.clear()
                    return "alternating"

        return None


def _canonicalize_dict(d: Mapping[str, Any] | None) -> str:
    """Stable JSON stringification for exact match comparisons."""
    if not d:
        return ""
    try:
        return json.dumps(d, sort_keys=True)
    except Exception:
        # Fallback for non-serializable objects (rare in ActionRecord but safe)
        return repr(d)


def _is_identical(r1: ActionRecord, r2: ActionRecord) -> bool:
    """Exact equality check for (argv, outcome) per acceptance criteria."""
    if r1.tool_name != r2.tool_name:
        return False
    if _canonicalize_dict(r1.tool_args) != _canonicalize_dict(r2.tool_args):
        return False
    return _canonicalize_dict(r1.tool_results) == _canonicalize_dict(r2.tool_results)
