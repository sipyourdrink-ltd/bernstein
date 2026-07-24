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

**Hermetic replay (issue #1832).** Replay is strict by default: a cache miss
raises :class:`ReplayMissError` and aborts the run rather than silently calling
the live model. This mirrors the contract of
:class:`bernstein.core.replay.gateway.ReplayMissError`. Operators who genuinely
want record-extend-on-miss behaviour must opt in with the
:data:`ALLOW_LIVE_MISS_ENV` environment flag, which downgrades misses to a
logged warning + live fall-through. The replay key folds in every
response-determining input (model, prompt, provider, temperature, max_tokens),
so a parameter drift can never masquerade as a hit. Widening the key
invalidates ``llm_calls.jsonl`` files recorded before #1832; re-record by
running with ``BERNSTEIN_DETERMINISTIC_SEED`` set again.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections import deque
from contextlib import suppress
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# Module-level active store (one per orchestrator subprocess).
_active_store: DeterministicStore | None = None

#: Environment flag that opts replay out of strict mode. When set to a truthy
#: value, a cache miss emits a WARNING and falls through to the live provider
#: (the pre-#1832 record-extend behaviour) instead of raising. Strict mode is
#: the default precisely so this hole stays closed unless deliberately opened.
ALLOW_LIVE_MISS_ENV = "BERNSTEIN_REPLAY_ALLOW_LIVE_MISS"

_TRUTHY = frozenset({"1", "true", "yes", "on"})

#: Default LLM parameters folded into the replay key. These mirror the
#: defaults of :func:`bernstein.core.routing.llm.call_llm`, so a re-recorded
#: run keys identically to a live run that takes the default path.
_DEFAULT_PROVIDER = "openrouter_free"
_DEFAULT_TEMPERATURE = 0.7
_DEFAULT_MAX_TOKENS = 4000


def allow_live_miss(env: dict[str, str] | None = None) -> bool:
    """Return whether replay may fall through to a live call on a miss.

    Args:
        env: Optional env mapping (defaults to :data:`os.environ`).

    Returns:
        ``True`` only when :data:`ALLOW_LIVE_MISS_ENV` is set to a truthy
        value. Replay is hermetic (strict) by default.
    """
    src = env if env is not None else os.environ
    return src.get(ALLOW_LIVE_MISS_ENV, "").strip().lower() in _TRUTHY


class ReplayMissError(RuntimeError):
    """Raised in strict replay mode when no recorded response matches.

    Mirrors :class:`bernstein.core.replay.gateway.ReplayMissError` so the two
    replay subsystems share one miss contract. Subclasses :class:`RuntimeError`
    so existing ``call_llm`` callers that catch ``RuntimeError`` propagate the
    abort rather than swallowing a fake response.

    Attributes:
        key: The widened prompt key that was looked up (never ``None``).
        model: The model identifier that was requested (never ``None``).
    """

    def __init__(self, key: str, model: str, *, run_dir: str | None = None) -> None:
        self.key = key
        self.model = model
        location = f" in {run_dir}/llm_calls.jsonl" if run_dir else ""
        super().__init__(
            f"Deterministic replay miss: no recorded response for model={model!r} "
            f"(key={key}){location}. Strict replay will not call the live model. "
            "Either the run diverged from the recording, a response-determining "
            "input (provider/temperature/max_tokens) drifted, or the recording "
            "predates the #1832 key widening and must be re-recorded. To re-record, "
            "re-run with BERNSTEIN_DETERMINISTIC_SEED set (and BERNSTEIN_REPLAY_RUN_ID "
            "unset). To allow live fall-through on misses (non-hermetic), set "
            f"{ALLOW_LIVE_MISS_ENV}=1.",
        )


class ReplayRecordingMissingError(RuntimeError):
    """Raised when strict replay is activated against a run with no recording.

    Extends the :class:`ReplayMissError` contract from the per-call site to the
    whole-run activation boundary (issue #2790). On the CLI-adapter path
    (``internal_llm_provider: none`` + a CLI agent) ``call_llm`` never runs, so
    no ``llm_calls.jsonl`` is written and a replay store loads zero cached
    responses. Activating replay against such a run would fall through to a live
    run - real agent subprocesses, real network calls - while the operator
    believes replay is hermetic. Callers raise this before any agent is spawned
    so a strict replay refuses loudly instead of degrading to live.

    Attributes:
        run_dir: The run directory whose recording is absent or empty.
    """

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        super().__init__(
            f"Deterministic replay requested but no recording exists at "
            f"{run_dir / 'llm_calls.jsonl'}. Strict/hermetic replay refuses to run "
            "live. This run recorded no LLM calls - the CLI-adapter path "
            "(internal_llm_provider 'none' + a CLI agent) does not record, so it "
            "cannot be replayed hermetically. To run anyway with live fall-through "
            f"(non-hermetic), set {ALLOW_LIVE_MISS_ENV}=1; to produce a recording, "
            "re-run with BERNSTEIN_DETERMINISTIC_SEED set and an internal LLM "
            "provider configured.",
        )


def _prompt_key(
    prompt: str,
    model: str,
    *,
    provider: str = _DEFAULT_PROVIDER,
    temperature: float = _DEFAULT_TEMPERATURE,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
) -> str:
    """Compute a stable lookup key for one LLM request.

    The key folds in every input that changes the model's response so a
    cache hit cannot mask a parameter drift (issue #1832). Widening the key
    invalidates ``llm_calls.jsonl`` files recorded before this change.

    Args:
        prompt: Full prompt string.
        model: Model identifier.
        provider: Provider name (e.g. ``"openrouter_free"``).
        temperature: Sampling temperature.
        max_tokens: Maximum response tokens.

    Returns:
        Hex-encoded SHA-256 over the NUL-separated request tuple.
    """
    # NUL separators keep field boundaries unambiguous; ``temperature`` is
    # formatted with ``repr`` so 0.7 and 0.70 hash identically while 0.7 and
    # 0.0 stay distinct.
    data = f"{model}\x00{prompt}\x00{provider}\x00{temperature!r}\x00{max_tokens}".encode()
    return hashlib.sha256(data).hexdigest()


class DeterministicStore:
    """Records LLM calls during a run and replays them for reproducibility.

    In *recording* mode (``replay=False``, the default), every call to
    :meth:`record` appends an entry to ``llm_calls.jsonl``.

    In *replay* mode (``replay=True``), the cache is pre-loaded from an
    existing ``llm_calls.jsonl`` and :meth:`get_replay` returns stored
    responses without touching the file.

    Replay preserves call **order and multiplicity** (issue #1846): the
    recording is append-only, so a ``(prompt, model, ...)`` key called N times
    records N responses in order. The cache keeps a per-key FIFO and
    :meth:`get_replay` consumes the next recorded response for a key, so the
    Nth call replays the Nth recorded response. Requesting a key more times
    than it was recorded is a replay-fidelity failure (the run diverged from
    the recording), handled by the strict/non-strict policy below.

    Replay is **strict by default** (issue #1832): a cache miss - including
    over-consuming a key past its recorded count - raises
    :class:`ReplayMissError` instead of returning ``None``, so a run launched
    for replay cannot silently call the live model or replay a stale response.
    Pass ``strict=False`` (the orchestrator does this when
    :func:`allow_live_miss` is set) to restore the old return-``None``-on-miss
    escape hatch.

    Args:
        run_dir: Directory for this run (``{sdd_dir}/runs/{run_id}``).
        replay: If ``True``, load and replay recorded responses instead of
            writing new ones.
        strict: When replaying, raise :class:`ReplayMissError` on a cache
            miss. Defaults to ``True`` and is ignored outside replay mode.
    """

    def __init__(self, run_dir: Path, *, replay: bool = False, strict: bool = True) -> None:
        self._run_dir = run_dir
        self._replay = replay
        self._strict = strict
        self._calls_path = run_dir / "llm_calls.jsonl"
        # Per-key FIFO of recorded responses, in recorded order. A repeated
        # ``(prompt, model, ...)`` key keeps every recorded response so replay
        # reconstructs the exact recorded sequence instead of last-write-wins
        # (issue #1846). ``get_replay`` consumes from the left.
        self._cache: dict[str, deque[str]] = {}
        # Total recorded responses retained across all keys; backs
        # :attr:`cached_count` so it still reflects the journal size.
        self._cached_total = 0
        # Replay-coverage counters backing :meth:`coverage_line`.
        self._hits = 0
        self._misses = 0
        self._strict_violations = 0
        run_dir.mkdir(parents=True, exist_ok=True)
        if replay and self._calls_path.exists():
            self._load_cache()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_cache(self) -> None:
        """Load recorded responses into per-key FIFO queues for replay.

        Appends each recorded response to its key's queue in file order, so a
        key recorded N times replays its N responses in sequence (#1846).
        """
        try:
            with self._calls_path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    with suppress(json.JSONDecodeError, KeyError):
                        entry: dict[str, Any] = json.loads(line)
                        key = entry.get("key", "")
                        response = entry.get("response", "")
                        if key and response:
                            self._cache.setdefault(key, deque()).append(response)
                            self._cached_total += 1
        except OSError as exc:
            logger.warning("DeterministicStore: failed to load cache: %s", exc)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record(
        self,
        prompt: str,
        model: str,
        response: str,
        *,
        provider: str = _DEFAULT_PROVIDER,
        temperature: float = _DEFAULT_TEMPERATURE,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
    ) -> None:
        """Append an LLM call record to the JSONL store.

        No-op when in replay mode. The persisted key folds in
        ``provider``/``temperature``/``max_tokens`` so a later strict replay
        matches only when every response-determining input is identical.

        Args:
            prompt: Full prompt sent to the LLM.
            model: Model identifier (e.g. ``"claude-3-5-sonnet"``).
            response: Response returned by the LLM.
            provider: Provider name used for the call.
            temperature: Sampling temperature used for the call.
            max_tokens: Max response tokens used for the call.
        """
        if self._replay:
            return
        key = _prompt_key(prompt, model, provider=provider, temperature=temperature, max_tokens=max_tokens)
        entry: dict[str, Any] = {
            "ts": time.time(),
            "key": key,
            "model": model,
            "provider": provider,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "prompt_len": len(prompt),
            "response": response,
        }
        try:
            with self._calls_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except OSError as exc:
            logger.warning("DeterministicStore: failed to record LLM call: %s", exc)

    def get_replay(
        self,
        prompt: str,
        model: str,
        *,
        provider: str = _DEFAULT_PROVIDER,
        temperature: float = _DEFAULT_TEMPERATURE,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
    ) -> str | None:
        """Return a cached response, or signal a miss.

        Args:
            prompt: Full prompt string.
            model: Model identifier.
            provider: Provider name (folded into the lookup key).
            temperature: Sampling temperature (folded into the lookup key).
            max_tokens: Max response tokens (folded into the lookup key).

        Returns:
            The next recorded response for the key on a hit (consuming it in
            recorded order). Returns ``None`` when the store is not in replay
            mode, or on a miss in non-strict replay mode. A miss includes both
            an unknown key and over-consuming a known key past its recorded
            count.

        Raises:
            ReplayMissError: On a cache miss when the store is in strict
                replay mode (the default). The error carries the prompt key
                and model.
        """
        if not self._replay:
            return None
        key = _prompt_key(prompt, model, provider=provider, temperature=temperature, max_tokens=max_tokens)
        queue = self._cache.get(key)
        if queue:
            self._hits += 1
            return queue.popleft()
        # Cache miss: unknown key, or a known key consumed past its recorded
        # count (the run requested it more times than it was recorded).
        if self._strict:
            self._strict_violations += 1
            # Emit the coverage line at the failure point so the abort log
            # carries a verifiable summary of what the replay consumed.
            logger.error("DeterministicStore: strict replay miss; %s", self.coverage_line())
            raise ReplayMissError(key, model, run_dir=str(self._run_dir))
        self._misses += 1
        return None

    @property
    def is_replay(self) -> bool:
        """Whether this store is in replay (read-only) mode."""
        return self._replay

    @property
    def is_strict(self) -> bool:
        """Whether replay misses raise instead of falling through."""
        return self._strict

    @property
    def hits(self) -> int:
        """Count of replay lookups served from the recording."""
        return self._hits

    @property
    def misses(self) -> int:
        """Count of non-strict replay misses (live fall-through)."""
        return self._misses

    @property
    def strict_violations(self) -> int:
        """Count of strict replay misses (each aborts the run)."""
        return self._strict_violations

    @property
    def cached_count(self) -> int:
        """Total recorded responses loaded for replay (across all keys).

        Counts every recorded response, including repeats of the same key, so
        it reflects the journal size rather than the number of distinct keys.
        """
        return self._cached_total

    @property
    def calls_path(self) -> Path:
        """Path to the ``llm_calls.jsonl`` file."""
        return self._calls_path

    def coverage_line(self) -> str:
        """Return a one-line replay-coverage summary for operators.

        A fully-covered hermetic replay reports ``misses=0`` and
        ``strict_violations=0``, so an operator can confirm the run consumed
        100% recorded responses.
        """
        return (
            f"replay-coverage run_dir={self._run_dir} cached={self._cached_total} "
            f"hits={self._hits} misses={self._misses} "
            f"strict_violations={self._strict_violations} strict={self._strict}"
        )


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


def open_replay_store(run_dir: Path, *, strict: bool) -> DeterministicStore:
    """Open a replay store for ``run_dir``, refusing a strict miss.

    A strict/hermetic replay must never fall through to a live model or a live
    agent spawn (issue #2790). When ``strict`` is set and the run recorded no
    LLM calls (absent or empty ``llm_calls.jsonl`` -> ``cached_count == 0``),
    raise :class:`ReplayRecordingMissingError` so the caller aborts before
    spawning any agent, mirroring the :class:`ReplayMissError` contract enforced
    per-call inside ``call_llm``. Non-strict replay (live-miss allowed) opens
    the store even when empty, preserving the record-extend workflow.

    Args:
        run_dir: The run directory (``{sdd_dir}/runs/{run_id}``) to replay.
        strict: Whether replay is hermetic (refuse on miss).

    Returns:
        The replay store when a recording is present, or when non-strict.

    Raises:
        ReplayRecordingMissingError: Strict replay with no recording.
    """
    store = DeterministicStore(run_dir, replay=True, strict=strict)
    if strict and store.cached_count == 0:
        raise ReplayRecordingMissingError(run_dir)
    return store


def load_replay_store(run_id: str, sdd_dir: Path) -> DeterministicStore:
    """Create a DeterministicStore in replay mode for the given run.

    Replay is hermetic (strict) unless :data:`ALLOW_LIVE_MISS_ENV` is set, in
    which case misses fall through to the live provider with a warning. A strict
    replay against a run with no recording refuses via
    :class:`ReplayRecordingMissingError` rather than degrading to a live run
    (issue #2790).

    Args:
        run_id: Run ID whose ``llm_calls.jsonl`` should be replayed.
        sdd_dir: Path to the ``.sdd`` directory.

    Returns:
        Store loaded with cached responses from the specified run.

    Raises:
        ReplayRecordingMissingError: Strict replay with no recording.
    """
    run_dir = sdd_dir / "runs" / run_id
    return open_replay_store(run_dir, strict=not allow_live_miss())
