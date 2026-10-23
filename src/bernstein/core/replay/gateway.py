"""Record/replay gateway for LLM requests and tool dispatch.

The gateway sits between Bernstein's adapter call-sites and the live
providers. In *record* mode every (kind, key) -> response pair is appended
to ``.sdd/runs/<run_id>/events.jsonl``. In *replay* mode the gateway
serves the recorded response instead of invoking the real provider, so a
run can be re-executed deterministically against recorded fixtures.

Design choices:

* **Append-only JSONL** - same on-disk shape as existing trace files; works
  with the rest of the observability stack and stays human-diffable.
* **Recording is opt-in** - controlled by :data:`RECORD_ENV_VAR` or an
  explicit ``record=True`` argument. We don't want to bloat ``.sdd/`` for
  users who never replay.
* **Stable keys** - callers pass an explicit ``key`` (typically a SHA-256
  of the request payload). The gateway never tries to fingerprint the
  request itself; key stability is the caller's job. On the store
  boundary the caller key is rewritten to a scheme-prefixed digest
  (``v1:<64 hex>``) so a later derivation change can classify old rows
  instead of reporting false divergences (#4867).
* **Same-scheme key miss is a divergence** - replay looks up
  ``(kind, key)`` and raises :class:`ReplayDivergenceError` without
  consuming the queue when the key is absent (#4866). Set
  ``BERNSTEIN_REPLAY_LENIENT=1`` to restore by-kind FIFO; each
  consumption logs one line.
"""

from __future__ import annotations

import json
import logging
import operator
import os
import threading
import time
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, NoReturn

from bernstein.core.replay.key_scheme import (
    CURRENT_KEY_SCHEME,
    derive_replay_key,
    parse_stored_key,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

logger = logging.getLogger(__name__)

#: Name of the per-run gateway event log inside ``.sdd/runs/<id>/``.
EVENTS_FILENAME = "events.jsonl"

#: Environment variable that opts the gateway into record mode.
#: Recording stays off by default to avoid growing ``.sdd/`` on every
#: invocation. Set to ``1``/``true``/``yes`` to enable.
RECORD_ENV_VAR = "BERNSTEIN_RECORD"

#: Opt-in that restores by-kind FIFO when a same-scheme key misses (#4866).
LENIENT_ENV_VAR = "BERNSTEIN_REPLAY_LENIENT"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def is_recording_enabled(env: dict[str, str] | None = None) -> bool:
    """Return whether the gateway should record this run by default.

    Args:
        env: Optional env dict (defaults to :data:`os.environ`).

    Returns:
        ``True`` if :data:`RECORD_ENV_VAR` is set to a truthy value.
    """
    src = env if env is not None else os.environ
    return src.get(RECORD_ENV_VAR, "").strip().lower() in _TRUTHY


def is_replay_lenient(env: dict[str, str] | None = None) -> bool:
    """Return whether a same-scheme key miss should fall back to FIFO.

    Args:
        env: Optional env dict (defaults to :data:`os.environ`).

    Returns:
        ``True`` if :data:`LENIENT_ENV_VAR` is set to a truthy value.
    """
    src = env if env is not None else os.environ
    return src.get(LENIENT_ENV_VAR, "").strip().lower() in _TRUTHY


class GatewayMode(StrEnum):
    """Operating mode for :class:`ReplayGateway`."""

    OFF = "off"
    """Pass-through; no recording, no replay."""

    RECORD = "record"
    """Invoke the live provider and append each response to ``events.jsonl``."""

    REPLAY = "replay"
    """Serve recorded responses; never call the live provider."""


class ReplayMissError(RuntimeError):
    """Raised in :attr:`GatewayMode.REPLAY` when the run recorded nothing.

    A same-scheme key miss against a non-empty corpus is
    :class:`ReplayDivergenceError`, not this error. Lenient FIFO still
    raises this once every fixture of the kind has been consumed.
    """


class ReplayDivergenceError(RuntimeError):
    """A same-scheme replay key missed, so the recorded queue was not consumed.

    ``got_key`` and ``expected_key`` use the scheme-prefixed storage form
    (``vN:<64 hex>``). ``expected_key`` is the next recorded key when
    events of this kind remain, and ``None`` when that recording is
    exhausted.
    """

    def __init__(
        self,
        *,
        kind: str,
        got_key: str,
        expected_key: str | None,
    ) -> None:
        self.kind = kind
        self.got_key = got_key
        self.expected_key = expected_key
        if expected_key is None:
            message = (
                f"replay divergence: kind={kind!r} got key={got_key!r}; "
                "recording exhausted (no remaining events of this kind)"
            )
        else:
            message = f"replay divergence: kind={kind!r} got key={got_key!r} expected key={expected_key!r}"
        super().__init__(message)


class ReplayKeySchemeMismatchError(RuntimeError):
    """Raised when a corpus was recorded under a different key scheme.

    Distinct from :class:`ReplayMissError`: the rows are present, but their
    stored keys were derived under another scheme, so comparing them as
    divergences would be a false signal. Re-record under the current scheme
    to compare. Chosen as an exception (not a return verdict) so ``dispatch``
    keeps returning the response payload and #4866's miss path can follow the
    same raise-on-failure shape.
    """

    def __init__(self, *, recorded_scheme: str, current_scheme: str) -> None:
        self.recorded_scheme = recorded_scheme
        self.current_scheme = current_scheme
        super().__init__(
            f"recorded under scheme {recorded_scheme}, current is {current_scheme} — re-record to compare",
        )


@dataclass(frozen=True)
class _Event:
    """One row from ``events.jsonl``."""

    kind: str
    key: str
    response: Any
    ts: float
    seq: int


@dataclass
class _Fixture:
    """A recorded response plus its consumption state during replay.

    Holds the recorded ``response`` and a ``consumed`` flag. The fixture lives
    in exactly one per-kind ordered list (recorded order); the by-key index
    references it by position, so a by-key consume and a by-kind consume mark
    the same object - the two views can never disagree on which recorded slot
    was served (#1855).
    """

    response: Any
    stored_key: str = ""
    consumed: bool = field(default=False)


class ReplayGateway:
    """Thin wrapper around LLM + tool dispatch with record/replay support.

    Typical usage from an adapter call-site::

        gw = ReplayGateway(run_id="20260517-1530", sdd_dir=Path(".sdd"))
        text = gw.dispatch(
            kind="llm",
            key=request_hash,
            invoke=lambda: real_llm_client.complete(prompt),
        )

    With ``BERNSTEIN_RECORD=1`` (or ``ReplayGateway(record=True)``) the
    response from ``invoke`` is appended to ``events.jsonl``. In replay
    mode, ``invoke`` is **not** called; the recorded response is returned
    instead.

    Args:
        run_id: Unique identifier for this run; used to locate the
            per-run event log under ``.sdd/runs/<run_id>/``.
        sdd_dir: Path to the ``.sdd`` directory.
        mode: Explicit :class:`GatewayMode`. If omitted, defaults to
            :attr:`GatewayMode.RECORD` when :func:`is_recording_enabled`
            is true and ``record`` is not set, else :attr:`GatewayMode.OFF`.
        record: Convenience flag - when ``True``, forces record mode even
            if the env var is unset. Ignored if ``mode`` is provided.
        key_scheme: Key-derivation scheme written on record and required on
            replay (default :data:`~bernstein.core.replay.key_scheme.CURRENT_KEY_SCHEME`).
            Tests pass ``v2`` (etc.) to exercise cross-scheme classification.
        lenient: When ``True``, a same-scheme key miss consumes the next
            recorded fixture of that kind (the pre-#4866 FIFO fallback) and
            logs one line. When ``None`` (the default), follow
            :func:`is_replay_lenient`. When ``False``, a miss raises
            :class:`ReplayDivergenceError` and leaves the queue unconsumed.
    """

    def __init__(
        self,
        run_id: str,
        sdd_dir: Path,
        *,
        mode: GatewayMode | None = None,
        record: bool = False,
        key_scheme: str | None = None,
        lenient: bool | None = None,
    ) -> None:
        self._run_id = run_id
        self._path = sdd_dir / "runs" / run_id / EVENTS_FILENAME
        self._lock = threading.Lock()
        self._seq = 0
        self._key_scheme = CURRENT_KEY_SCHEME if key_scheme is None else key_scheme
        self._lenient = is_replay_lenient() if lenient is None else lenient

        if mode is None:
            mode = GatewayMode.RECORD if record or is_recording_enabled() else GatewayMode.OFF
        self._mode = mode

        # Replay-mode fixture state. A single ordered list per kind is the
        # source of truth (recorded order); the by-key index points into it by
        # position, so consuming a fixture by key and by kind can never desync
        # even when distinct keys recorded identical response values (#1855).
        self._ordered_by_kind: dict[str, list[_Fixture]] = {}
        self._positions_by_key: dict[tuple[str, str], deque[int]] = {}
        # Per-kind cursor: index of the first not-yet-consumed fixture, so the
        # by-kind FIFO fallback is amortised O(1) instead of rescanning.
        self._kind_cursor: dict[str, int] = {}
        # Schemes observed in the loaded corpus (``None`` = unversioned row).
        self._corpus_schemes: set[str | None] = set()

        if self._mode is GatewayMode.RECORD:
            # Only create the directory when we'll actually write something.
            # Replay mode reads existing files; OFF mode does nothing.
            self._path.parent.mkdir(parents=True, exist_ok=True)
        elif self._mode is GatewayMode.REPLAY:
            self._load_fixtures()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def mode(self) -> GatewayMode:
        """Current operating mode."""
        return self._mode

    @property
    def path(self) -> Path:
        """Path to ``events.jsonl`` for this run."""
        return self._path

    @property
    def run_id(self) -> str:
        """The run identifier this gateway targets."""
        return self._run_id

    def dispatch(
        self,
        *,
        kind: str,
        key: str,
        invoke: Callable[[], Any],
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        """Run a recorded/replayed dispatch.

        Args:
            kind: Logical category (e.g. ``"llm"``, ``"tool"``). Used to
                bucket replay fixtures when keys collide.
            key: Stable identifier for this request (typically a hash of
                the request payload). Replay serves an exact ``(kind, key)``
                hit. A same-scheme miss raises
                :class:`ReplayDivergenceError` unless lenient FIFO is on.
            invoke: Callable that performs the real dispatch. Called in
                :attr:`GatewayMode.OFF` and :attr:`GatewayMode.RECORD`;
                **never** called in :attr:`GatewayMode.REPLAY`.
            metadata: Optional extra fields persisted alongside the event
                (e.g. model name, adapter name) for debugging.

        Returns:
            The response (either from ``invoke`` or from the fixture).

        Raises:
            ReplayMissError: In replay mode when this run recorded no
                events, or (lenient mode only) when the kind's queue is
                already exhausted.
            ReplayDivergenceError: In replay mode when the key misses a
                same-scheme corpus. The recorded queue is not consumed.
            ReplayKeySchemeMismatchError: In replay mode when the loaded
                corpus was recorded under a different key scheme than this
                verifier (re-record to compare; not a divergence).
        """
        if self._mode is GatewayMode.REPLAY:
            return self._replay_lookup(kind=kind, key=key)

        response = invoke()

        if self._mode is GatewayMode.RECORD:
            self._record(kind=kind, key=key, response=response, metadata=metadata)

        return response

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def _record(
        self,
        *,
        kind: str,
        key: str,
        response: Any,
        metadata: dict[str, Any] | None,
    ) -> None:
        """Append one event to ``events.jsonl``.

        Sequence assignment AND the file write happen under ``self._lock``.
        Splitting these two steps (lock-then-release before opening the
        file) let two concurrent record calls swap their ``seq`` order in
        the file, and worse - concurrent file writes past PIPE_BUF can
        interleave bytes, producing malformed JSONL the replay loader
        then silently skips. The lock is local to the gateway, so the
        critical section is short and uncontended for typical adapter
        traffic.
        """
        entry: dict[str, Any] = {
            "ts": time.time(),
            "kind": kind,
            "key": derive_replay_key(key, scheme=self._key_scheme),
            "response": _make_jsonable(response),
        }
        if metadata:
            entry["metadata"] = _make_jsonable(metadata)

        with self._lock:
            self._seq += 1
            entry["seq"] = self._seq
            # ``json.dumps`` runs inside the lock so the ``seq`` field is
            # consistent with the file order; the lock also serialises the
            # subsequent file.write so two concurrent records can never
            # interleave bytes past PIPE_BUF.
            line = json.dumps(entry, default=str)
            try:
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError as exc:
                # Recording is a debug aid; failures must not break the run.
                logger.warning("ReplayGateway: failed to record %r: %s", kind, exc)

    # ------------------------------------------------------------------
    # Replay
    # ------------------------------------------------------------------

    def _load_fixtures(self) -> None:
        """Load fixtures from ``events.jsonl`` into ordered per-kind lists.

        Events are ordered by their recorded ``seq`` so the by-kind FIFO
        fallback replays in recorded order regardless of duplicate response
        values. Rows missing ``seq`` (legacy logs predating the per-event
        sequence) fall back to file order, which is the implicit recorded
        order. The by-key index records each fixture's *position* in its
        kind's ordered list, so a by-key consume marks the exact recorded
        slot rather than the first slot with a matching value (#1855).
        """
        if not self._path.exists():
            raise ReplayMissError(
                f"No events log at {self._path}; nothing to replay. "
                "Was BERNSTEIN_RECORD=1 set during the original run?",
            )
        # encoding="utf-8" mirrors the record path; relying on the platform
        # default broke replay on Windows runners where cp1252 was active.
        rows: list[tuple[int, int, str, str, Any]] = []
        with self._path.open(encoding="utf-8") as f:
            for file_pos, raw in enumerate(f):
                row_str = raw.strip()
                if not row_str:
                    continue
                try:
                    row = json.loads(row_str)
                except json.JSONDecodeError:
                    logger.warning("ReplayGateway: skipping malformed line in %s", self._path)
                    continue
                kind = str(row.get("kind", ""))
                key = str(row.get("key", ""))
                response = row.get("response")
                # ``seq`` is the recorded order; legacy logs without it use file
                # order. ``file_pos`` is the stable tiebreak so two rows sharing
                # a seq (or both missing it) keep their on-disk order.
                seq_raw = row.get("seq")
                seq = int(seq_raw) if isinstance(seq_raw, int) else file_pos
                rows.append((seq, file_pos, kind, key, response))

        # Sort by (seq, file_pos) so the per-kind lists are in recorded order
        # even if the log was written or stitched out of strict line order.
        rows.sort(key=operator.itemgetter(0, 1))
        for _seq, _pos, kind, key, response in rows:
            scheme, _digest = parse_stored_key(key)
            self._corpus_schemes.add(scheme)
            ordered = self._ordered_by_kind.setdefault(kind, [])
            fixture = _Fixture(response=response, stored_key=key)
            position = len(ordered)
            ordered.append(fixture)
            self._positions_by_key.setdefault((kind, key), deque()).append(position)

    def _reject_scheme_mismatch(self) -> None:
        """Raise when the loaded corpus is not under this verifier's scheme.

        Must run before by-key or FIFO consume so an older-scheme corpus never
        silently falls through to misaligned fixtures (#4867).
        """
        if self._corpus_schemes == {self._key_scheme}:
            return
        labels = sorted("unversioned" if scheme is None else scheme for scheme in self._corpus_schemes)
        recorded = ",".join(labels) if labels else "none"
        raise ReplayKeySchemeMismatchError(
            recorded_scheme=recorded,
            current_scheme=self._key_scheme,
        )

    def _next_unconsumed_index(self, kind: str, ordered: list[_Fixture]) -> int | None:
        """Return the index of the lowest unconsumed fixture for ``kind``.

        Advances and caches a per-kind cursor past already-consumed fixtures
        so repeated by-kind fallbacks stay amortised O(1). Returns ``None``
        when every fixture for the kind has been consumed.
        """
        cursor = self._kind_cursor.get(kind, 0)
        while cursor < len(ordered) and ordered[cursor].consumed:
            cursor += 1
        self._kind_cursor[kind] = cursor
        return cursor if cursor < len(ordered) else None

    def _peek_unconsumed_index(self, kind: str, ordered: list[_Fixture]) -> int | None:
        """Return the next unconsumed index without moving the kind cursor."""
        cursor = self._kind_cursor.get(kind, 0)
        while cursor < len(ordered) and ordered[cursor].consumed:
            cursor += 1
        return cursor if cursor < len(ordered) else None

    def _replay_lookup(self, *, kind: str, key: str) -> Any:
        """Consume the recorded fixture for ``(kind, key)``.

        A by-key hit consumes that recorded slot and returns its response.
        The bytes on that path are the recorded response, unchanged (#4866
        outcome 2). A same-scheme miss raises :class:`ReplayDivergenceError`
        and does not mark any fixture consumed, so the kind cursor stays
        where it was. Lenient mode restores by-kind FIFO and logs one line
        per consumption.

        Holds ``self._lock`` for the entire decision so concurrent dispatches
        cannot drain the same fixture twice.
        """
        storage_key = derive_replay_key(key, scheme=self._key_scheme)
        with self._lock:
            self._reject_scheme_mismatch()
            ordered = self._ordered_by_kind.get(kind)
            if ordered is None:
                self._raise_kind_exhausted(kind, storage_key)

            positions = self._positions_by_key.get((kind, storage_key))
            # Skip positions already consumed via lenient FIFO so a by-key
            # hit never returns a slot that was served as filler.
            while positions:
                idx = positions[0]
                if ordered[idx].consumed:
                    positions.popleft()
                    continue
                positions.popleft()
                ordered[idx].consumed = True
                return ordered[idx].response

            fallback_idx = self._peek_unconsumed_index(kind, ordered)
            if fallback_idx is None:
                self._raise_kind_exhausted(kind, storage_key)

            next_fixture = ordered[fallback_idx]
            if not self._lenient:
                raise ReplayDivergenceError(
                    kind=kind,
                    got_key=storage_key,
                    expected_key=next_fixture.stored_key,
                )

            logger.warning(
                "%s: FIFO fallback kind=%s got_key=%s expected_key=%s",
                LENIENT_ENV_VAR,
                kind,
                storage_key,
                next_fixture.stored_key,
            )
            # Commit the cursor only when a fixture is actually consumed.
            self._kind_cursor[kind] = fallback_idx
            next_fixture.consumed = True
            return next_fixture.response

    def _raise_kind_exhausted(self, kind: str, storage_key: str) -> NoReturn:
        """Raise the exhausted-recording verdict. Never returns."""
        if self._lenient:
            raise ReplayMissError(
                f"No fixture for kind={kind!r} key={storage_key!r} in {self._path}. "
                "Either the run diverged or recording was incomplete.",
            )
        raise ReplayDivergenceError(kind=kind, got_key=storage_key, expected_key=None)


def _make_jsonable(value: Any) -> Any:
    """Best-effort coercion of ``value`` to JSON-serialisable shape.

    Falls back to ``repr`` for opaque objects; primitive types and
    standard containers are passed through unchanged.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_make_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _make_jsonable(v) for k, v in value.items()}
    # Dataclasses, pydantic, custom objects: try dict-like, then repr.
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        with suppress(TypeError, ValueError):
            return _make_jsonable(to_dict())
    return repr(value)
