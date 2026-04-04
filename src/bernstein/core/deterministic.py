"""Deterministic run reproducibility via LLM response recording and replay.

Records every LLM call (prompt + model → response) during an orchestration run
to ``.sdd/runs/{run_id}/llm_calls.jsonl``.  A subsequent run with the same seed
+ codebase can replay those cached responses instead of calling the LLM again,
producing an identical task decomposition.

Workflow::

    # Normal run (recording):
    # bernstein run --seed 42

    # Reproduce run (replaying):
    # bernstein replay <run_id> --reproduce
    # → sets BERNSTEIN_REPLAY_RUN_ID, reruns orchestrator with cached responses

The deterministic seed is applied to Python's ``random`` module so that routing
decisions using ``random.choice`` / ``random.random`` are identical across runs
with the same seed value.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# Module-level active store (one per orchestrator subprocess).
_active_store: DeterministicStore | None = None


def _prompt_key(prompt: str, model: str) -> str:
    """Compute a stable lookup key for a (prompt, model) pair.

    Args:
        prompt: Full prompt string.
        model: Model identifier.

    Returns:
        Hex-encoded SHA-256 of ``model\\x00prompt``.
    """
    data = f"{model}\x00{prompt}".encode()
    return hashlib.sha256(data).hexdigest()


class DeterministicStore:
    """Records LLM calls during a run and replays them for reproducibility.

    In *recording* mode (``replay=False``, the default), every call to
    :meth:`record` appends an entry to ``llm_calls.jsonl``.

    In *replay* mode (``replay=True``), the cache is pre-loaded from an
    existing ``llm_calls.jsonl`` and :meth:`get_replay` returns stored
    responses without touching the file.

    Args:
        run_dir: Directory for this run (``{sdd_dir}/runs/{run_id}``).
        replay: If ``True``, load and replay recorded responses instead of
            writing new ones.
    """

    def __init__(self, run_dir: Path, *, replay: bool = False) -> None:
        self._run_dir = run_dir
        self._replay = replay
        self._calls_path = run_dir / "llm_calls.jsonl"
        self._cache: dict[str, str] = {}
        run_dir.mkdir(parents=True, exist_ok=True)
        if replay and self._calls_path.exists():
            self._load_cache()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_cache(self) -> None:
        """Load all recorded responses into memory for fast replay."""
        try:
            with self._calls_path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry: dict[str, Any] = json.loads(line)
                        key = entry.get("key", "")
                        response = entry.get("response", "")
                        if key and response:
                            self._cache[key] = response
                    except (json.JSONDecodeError, KeyError):
                        pass
        except OSError as exc:
            logger.warning("DeterministicStore: failed to load cache: %s", exc)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record(self, prompt: str, model: str, response: str) -> None:
        """Append an LLM call record to the JSONL store.

        No-op when in replay mode.

        Args:
            prompt: Full prompt sent to the LLM.
            model: Model identifier (e.g. ``"claude-3-5-sonnet"``).
            response: Response returned by the LLM.
        """
        if self._replay:
            return
        key = _prompt_key(prompt, model)
        entry: dict[str, Any] = {
            "ts": time.time(),
            "key": key,
            "model": model,
            "prompt_len": len(prompt),
            "response": response,
        }
        try:
            with self._calls_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except OSError as exc:
            logger.warning("DeterministicStore: failed to record LLM call: %s", exc)

    def get_replay(self, prompt: str, model: str) -> str | None:
        """Return a cached response if in replay mode and a match exists.

        Args:
            prompt: Full prompt string.
            model: Model identifier.

        Returns:
            Cached response string, or ``None`` if not in replay mode or
            no matching record was found.
        """
        if not self._replay:
            return None
        key = _prompt_key(prompt, model)
        return self._cache.get(key)

    @property
    def is_replay(self) -> bool:
        """Whether this store is in replay (read-only) mode."""
        return self._replay

    @property
    def cached_count(self) -> int:
        """Number of cached responses available for replay."""
        return len(self._cache)

    @property
    def calls_path(self) -> Path:
        """Path to the ``llm_calls.jsonl`` file."""
        return self._calls_path


# ---------------------------------------------------------------------------
# Module-level store management (one store per orchestrator process)
# ---------------------------------------------------------------------------


def get_active_store() -> DeterministicStore | None:
    """Return the currently active DeterministicStore, or ``None``.

    Returns:
        Active store, or ``None`` if deterministic mode is not enabled.
    """
    return _active_store


def set_active_store(store: DeterministicStore | None) -> None:
    """Set the module-level active store for this process.

    Args:
        store: Store to activate, or ``None`` to disable.
    """
    global _active_store
    _active_store = store


def load_replay_store(run_id: str, sdd_dir: Path) -> DeterministicStore:
    """Create a DeterministicStore in replay mode for the given run.

    Args:
        run_id: Run ID whose ``llm_calls.jsonl`` should be replayed.
        sdd_dir: Path to the ``.sdd`` directory.

    Returns:
        Store loaded with cached responses from the specified run.
    """
    run_dir = sdd_dir / "runs" / run_id
    return DeterministicStore(run_dir, replay=True)
