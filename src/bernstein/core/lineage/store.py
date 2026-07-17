"""Append-only lineage log + rebuildable projections.

``LineageStore`` owns the on-disk layout described in ADR-009 §4:

  ``.sdd/lineage/``
    ├── ``log.jsonl``                 - source of truth, append-only
    ├── ``by-artefact/<aa>/<full>.jsonl``
    ├── ``tips/<full>.json``
    └── ``signatures/<aa>/<full>/<entry-hash>.jws``

Where ``<full>`` is ``sha256(artefact_path)`` and ``<aa>`` is its first two
hex characters.

Crash-safety contract:

* Every ``append`` takes ``fcntl.flock(LOCK_EX)`` over ``log.jsonl`` for the
  whole sequence of writes (log line → projection line → tip file → signature
  sidecar) so concurrent writers cannot interleave bytes within a record or
  observe a half-written projection.
* The log file descriptor is ``os.fsync``'d before ``append`` returns.
* Tip files are written via a ``tempfile`` + ``os.replace`` dance so a crash
  mid-write can never produce a torn JSON document at the canonical name.

Everything except ``log.jsonl`` is rebuildable: ``reindex`` re-derives
``by-artefact/`` and ``tips/`` from the log alone.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

# ``fcntl`` is POSIX-only. On Windows the module doesn't exist; the lock
# context manager below becomes a no-op (Windows CI runs are single-process
# so cross-process serialisation isn't load-bearing for our tests). Real
# multi-process safety on Windows would route through ``msvcrt.locking`` -
# wire that in when the orchestrator actually runs on Windows in anger.
if sys.platform == "win32":
    fcntl = None  # type: ignore[assignment]
else:
    import fcntl  # type: ignore[no-redef]

from bernstein.core.lineage.entry import LineageEntry, canonicalise, entry_hash

if TYPE_CHECKING:
    from collections.abc import Iterator

_LOG_NAME = "log.jsonl"
_BY_ARTEFACT_DIR = "by-artefact"
_TIPS_DIR = "tips"
_SIGNATURES_DIR = "signatures"


def _hash_path(artefact_path: str) -> tuple[str, str]:
    """Return (shard, full-hex) for ``sha256(artefact_path.utf-8)``.

    The shard is the first two hex characters - keeps directory fanout bounded
    even when a repo has many artefacts.
    """
    h = hashlib.sha256(artefact_path.encode("utf-8")).hexdigest()
    return h[:2], h


def _atomic_write_text(target: Path, payload: str) -> None:
    """Write ``payload`` to ``target`` via tempfile + ``os.replace``.

    The tempfile lives in the same directory so ``os.replace`` is a same-fs
    rename - atomic on POSIX. ``fsync`` is called on the tempfile before
    rename so the rename either reveals fully-written bytes or nothing at all.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=target.name + ".", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        # Clean up the tempfile on any failure so we don't leave orphan files.
        with contextlib.suppress(FileNotFoundError):
            Path(tmp_name).unlink()
        raise


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[int]:
    """Yield an fd holding ``flock(LOCK_EX)`` on ``path``.

    The lock fd is distinct from the file we ultimately write to - this keeps
    the lock lifecycle clean even when the writer opens the log in append
    mode multiple times for fsync.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # ``os.open`` with ``O_CREAT`` to make the lockfile if absent. We lock the
    # log file itself (per the spec), so re-opening the same path here is fine.
    # 0o600 - lineage log holds signed audit entries; operator-only readable.
    # CodeQL py/overly-permissive-file flags 0o644 as world-readable.
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield fd
        finally:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _compute_tips_from_entries(entry_hashes_in_order: list[tuple[str, list[str]]]) -> dict[str, list[str]]:
    """Project a list of ``(entry_hash, parent_hashes)`` into an open/merged tip set.

    Semantics (ADR-009 §4, §6):

      * ``open`` - heads of the DAG: entries that no other entry has named as
        a parent.
      * ``merged`` - entries that were *fork siblings* (i.e. became unresolved
        tips at some point) and are now subsumed by a multi-parent merge
        entry. A normal linear successor does not populate ``merged``.

    Order is preserved so callers get stable output (useful for the tip JSON
    file's on-disk form).
    """
    open_set: list[str] = []
    seen_open: set[str] = set()
    merged_set: list[str] = []
    seen_merged: set[str] = set()

    for h, parents in entry_hashes_in_order:
        if h not in seen_open:
            open_set.append(h)
            seen_open.add(h)

        # Whether this entry is a merge (>=2 parents).
        is_merge = len(parents) >= 2

        for p in parents:
            if p in seen_open:
                open_set.remove(p)
                seen_open.discard(p)
                # Only record as ``merged`` when a multi-parent merge entry
                # consumed it. A plain linear successor just demotes the
                # parent silently - it was never a fork.
                if is_merge and p not in seen_merged:
                    merged_set.append(p)
                    seen_merged.add(p)

    return {"open": open_set, "merged": merged_set}


class LineageStore:
    """File-backed lineage store rooted at ``root`` (typically ``.sdd/lineage/``).

    The store is safe to share across threads and across processes - all
    state-modifying operations take ``flock(LOCK_EX)`` over ``log.jsonl``.
    """

    def __init__(self, root: Path) -> None:
        self.root: Path = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        # Eagerly create the projection roots so ``reindex`` does not need to
        # special-case a missing tree on a freshly-initialised store.
        (self.root / _BY_ARTEFACT_DIR).mkdir(parents=True, exist_ok=True)
        (self.root / _TIPS_DIR).mkdir(parents=True, exist_ok=True)
        (self.root / _SIGNATURES_DIR).mkdir(parents=True, exist_ok=True)

    # -- internal paths -----------------------------------------------------

    @property
    def log_path(self) -> Path:
        return self.root / _LOG_NAME

    def _projection_path(self, artefact_path: str) -> Path:
        shard, full = _hash_path(artefact_path)
        return self.root / _BY_ARTEFACT_DIR / shard / f"{full}.jsonl"

    def _tip_path(self, artefact_path: str) -> Path:
        _, full = _hash_path(artefact_path)
        return self.root / _TIPS_DIR / f"{full}.json"

    def _signature_path(self, artefact_path: str, entry_hash_str: str) -> Path:
        shard, full = _hash_path(artefact_path)
        # Strip the ``sha256:`` prefix from the filename so it matches the
        # auditor (``bernstein.core.lineage.gate._signature_path``) and so
        # the path stays portable: ``:`` is not a legal filename character
        # on Windows. See ADR-009 §5.2 for the canonical sidecar layout.
        stem = entry_hash_str.split(":", 1)[1] if ":" in entry_hash_str else entry_hash_str
        return self.root / _SIGNATURES_DIR / shard / full / f"{stem}.jws"

    # -- public API ---------------------------------------------------------

    def append(self, entry: LineageEntry, jws: str) -> str:
        """Append a new entry to the log and update all projections.

        Returns the entry's hash (``sha256:<hex>`` of the JCS-canonical bytes).
        """
        canonical = canonicalise(entry)
        h = entry_hash(entry)

        with _exclusive_lock(self.log_path):
            # 1. Append canonical bytes + newline to log.jsonl.
            with self.log_path.open("ab") as log_fh:
                log_fh.write(canonical + b"\n")
                log_fh.flush()
                os.fsync(log_fh.fileno())

            # 2. Append to the by-artefact projection (best-effort; rebuildable).
            proj = self._projection_path(entry.artefact_path)
            proj.parent.mkdir(parents=True, exist_ok=True)
            with proj.open("ab") as proj_fh:
                proj_fh.write(canonical + b"\n")
                proj_fh.flush()
                os.fsync(proj_fh.fileno())

            # 3. Update tip set atomically (write-then-rename).
            tips = self._recompute_tips_for(entry.artefact_path, also_include=(h, list(entry.parent_hashes)))
            _atomic_write_text(
                self._tip_path(entry.artefact_path),
                json.dumps(tips, sort_keys=True, separators=(",", ":")),
            )

            # 4. Write the signature sidecar.
            sig_path = self._signature_path(entry.artefact_path, h)
            sig_path.parent.mkdir(parents=True, exist_ok=True)
            sig_path.write_text(jws, encoding="utf-8")

        return h

    def read_log(self) -> Iterator[tuple[LineageEntry, str]]:
        """Iterate ``(entry, jws)`` pairs over the entire log.

        The sidecar JWS for each entry is read from the ``signatures/`` tree.
        Yields entries in append order. Empty / missing log → empty iterator.
        """
        if not self.log_path.exists():
            return
        for raw in self.log_path.read_bytes().rstrip(b"\n").split(b"\n"):
            if not raw:
                continue
            payload = json.loads(raw)
            entry = _entry_from_dict(payload)
            h = entry_hash(entry)
            sig_path = self._signature_path(entry.artefact_path, h)
            jws = sig_path.read_text(encoding="utf-8") if sig_path.exists() else ""
            yield entry, jws

    def tip_set(self, artefact_path: str) -> dict[str, list[str]]:
        """Return the current open/merged tip set for ``artefact_path``.

        Reads the on-disk tip file when present; otherwise computes from the
        projection. Returns an empty open/merged pair when the artefact has
        never been recorded.
        """
        tip_path = self._tip_path(artefact_path)
        if tip_path.exists():
            with contextlib.suppress(json.JSONDecodeError):
                return json.loads(tip_path.read_text(encoding="utf-8"))
        return self._recompute_tips_for(artefact_path)

    def reindex(self) -> None:
        """Rebuild ``by-artefact/`` and ``tips/`` from ``log.jsonl`` alone."""
        with _exclusive_lock(self.log_path):
            # Wipe + recreate the projection directories.
            import shutil

            for sub in (_BY_ARTEFACT_DIR, _TIPS_DIR):
                target = self.root / sub
                if target.exists():
                    shutil.rmtree(target)
                target.mkdir(parents=True, exist_ok=True)

            # Bucket entries by artefact path as we walk the log.
            by_path: dict[str, list[tuple[str, list[str], bytes]]] = {}
            if self.log_path.exists():
                for raw in self.log_path.read_bytes().rstrip(b"\n").split(b"\n"):
                    if not raw:
                        continue
                    payload = json.loads(raw)
                    entry = _entry_from_dict(payload)
                    h = entry_hash(entry)
                    by_path.setdefault(entry.artefact_path, []).append((h, list(entry.parent_hashes), raw))

            for artefact_path, entries in by_path.items():
                proj = self._projection_path(artefact_path)
                proj.parent.mkdir(parents=True, exist_ok=True)
                with proj.open("wb") as proj_fh:
                    for _, _, raw in entries:
                        proj_fh.write(raw + b"\n")
                    proj_fh.flush()
                    os.fsync(proj_fh.fileno())

                tips = _compute_tips_from_entries([(h, parents) for h, parents, _ in entries])
                _atomic_write_text(
                    self._tip_path(artefact_path),
                    json.dumps(tips, sort_keys=True, separators=(",", ":")),
                )

    # -- helpers ------------------------------------------------------------

    def _recompute_tips_for(
        self,
        artefact_path: str,
        *,
        also_include: tuple[str, list[str]] | None = None,
    ) -> dict[str, list[str]]:
        """Walk the by-artefact projection and recompute the tip set.

        ``also_include`` lets ``append`` factor in the just-added entry before
        its bytes have been re-read from disk - saves a redundant read.
        """
        proj = self._projection_path(artefact_path)
        sequence: list[tuple[str, list[str]]] = []
        if proj.exists():
            for raw in proj.read_bytes().rstrip(b"\n").split(b"\n"):
                if not raw:
                    continue
                payload = json.loads(raw)
                entry = _entry_from_dict(payload)
                h = entry_hash(entry)
                if also_include is not None and h == also_include[0]:
                    # Already represented by the in-memory tuple - skip the dupe.
                    continue
                sequence.append((h, list(entry.parent_hashes)))
        if also_include is not None:
            sequence.append(also_include)
        return _compute_tips_from_entries(sequence)


def _entry_from_dict(payload: dict[str, object]) -> LineageEntry:
    """Reconstruct a ``LineageEntry`` from its JSON dict form."""
    # All canonical-field names map 1:1 onto the dataclass kwargs. Cast through
    # ``asdict`` of a dataclass would be cyclic; we do it manually.
    return LineageEntry(
        v=int(payload["v"]),  # type: ignore[arg-type]
        artefact_path=str(payload["artefact_path"]),
        artefact_kind=str(payload["artefact_kind"]),
        content_hash=str(payload["content_hash"]),
        parent_hashes=list(payload["parent_hashes"]),  # type: ignore[arg-type]
        agent_id=str(payload["agent_id"]),
        agent_card_kid=str(payload["agent_card_kid"]),
        tool_call_id=str(payload["tool_call_id"]),
        span_id=str(payload["span_id"]),
        ts_ns=int(payload["ts_ns"]),  # type: ignore[arg-type]
        operator_hmac=str(payload["operator_hmac"]),
        # Additive (issue #2513): absent on pre-feature entries -> None, which
        # canonicalises back to the exact bytes read, so entry_hash round-trips.
        trust_class=(None if payload.get("trust_class") is None else str(payload["trust_class"])),
    )


__all__ = ["LineageStore"]
