"""Lineage v2 - two-layer storage with detached child bodies.

This module implements the v2 storage layout described in issue #1249:

  ``.sdd/lineage/v2/``
    ├── ``parent.jsonl``               - parent timeline (HMAC chained refs)
    └── ``children/<child_sha>.jsonl`` - detached child body chains

Each parent line is a lightweight ``ParentRef`` carrying only:

* ``task_id``       - the parent task this child belongs to
* ``child_run_id``  - the child run identifier
* ``parent_call_id``- the tool/call id that spawned the child
* ``summary``       - short, operator-readable summary string
* ``child_sha``     - sha256 of the canonical first child body line
                      (this is the content-addressed pointer that joins
                      the two layers)
* ``hmac``          - HMAC-SHA256 over JCS bytes of the entry minus the
                      ``hmac`` field, salted by the previous parent line
                      HMAC. Forms an append-only chain over the parent
                      timeline.

Each child body file (``children/<child_sha>.jsonl``) is a per-task
HMAC chain of ``ChildBody`` events. The first body line's HMAC chains
from the empty string; subsequent lines chain from the previous body
line's HMAC. The first body line's sha256-of-canonical-bytes is the
``child_sha`` recorded by the parent ref - so any tampering with the
detached body invalidates the parent timeline too.

Atomicity contract:

* ``append`` takes ``fcntl.flock(LOCK_EX)`` over ``parent.jsonl`` for
  the whole sequence so concurrent writers cannot interleave bytes
  within a record or stamp an inconsistent (parent-ref, child-body)
  pair.
* The child file is written and fsynced BEFORE the parent ref line is
  appended. If the process crashes after the child write but before
  the parent line lands, the orphan child body is detected by
  ``verify()`` and reported but does not poison the parent timeline.
* Parent ref bytes are fsynced before ``append`` returns.

Backwards-compat:

* v1 (``.sdd/lineage/log.jsonl`` + ``by-artefact/`` + ``tips/``) is
  untouched.
* v2 is opt-in via ``BERNSTEIN_LINEAGE_V2=1`` env var or
  ``bernstein.yaml`` ``lineage.version: 2`` (the wiring lives in the
  recorder/runtime; this module only owns storage primitives).
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import os
import sys
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if sys.platform == "win32":
    fcntl = None  # type: ignore[assignment]
else:
    import fcntl  # type: ignore[no-redef]

if TYPE_CHECKING:
    from collections.abc import Iterator

LINEAGE_V2_ENTRY_VERSION = 2

_PARENT_LOG_NAME = "parent.jsonl"
_CHILDREN_DIR = "children"

_DEFAULT_HMAC_KEY = b"bernstein-lineage-v2-default-key"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParentRef:
    """Lightweight parent-timeline pointer.

    Frozen + slots: the dataclass shape itself is canonical; no surprise
    extra attributes can mutate the byte form used by the HMAC chain.
    """

    v: int
    task_id: str
    child_run_id: str
    parent_call_id: str
    summary: str
    child_sha: str
    ts_ns: int
    prev_hmac: str
    hmac: str

    def __post_init__(self) -> None:
        if self.v != LINEAGE_V2_ENTRY_VERSION:
            raise ValueError(f"unsupported parent_ref version: {self.v}")
        if not self.task_id:
            raise ValueError("task_id must be non-empty")
        if not self.child_run_id:
            raise ValueError("child_run_id must be non-empty")
        if not self.child_sha.startswith("sha256:"):
            raise ValueError(f"child_sha must start with 'sha256:', got {self.child_sha!r}")


def _empty_payload() -> dict[str, Any]:
    return {}


@dataclass(frozen=True, slots=True)
class ChildBody:
    """Full child event payload stored detached from the parent timeline.

    The first body in a child file is the one whose canonical bytes hash
    yields the ``child_sha`` recorded by the parent ref - i.e. the join
    pointer. Subsequent body lines extend the per-child HMAC chain.
    """

    v: int
    task_id: str
    child_run_id: str
    seq: int
    kind: str
    payload: dict[str, Any] = field(default_factory=_empty_payload)
    ts_ns: int = 0
    prev_hmac: str = ""
    hmac: str = ""

    def __post_init__(self) -> None:
        if self.v != LINEAGE_V2_ENTRY_VERSION:
            raise ValueError(f"unsupported child_body version: {self.v}")
        if not self.task_id:
            raise ValueError("task_id must be non-empty")
        if not self.child_run_id:
            raise ValueError("child_run_id must be non-empty")
        if self.seq < 0:
            raise ValueError(f"seq must be >=0, got {self.seq}")
        if not self.kind:
            raise ValueError("kind must be non-empty")


# ---------------------------------------------------------------------------
# Canonicalisation helpers (RFC 8785 JCS, flat-object subset)
# ---------------------------------------------------------------------------


def _canonicalise(d: dict[str, Any]) -> bytes:
    """RFC 8785-compatible canonical JSON bytes for a flat dict."""
    return json.dumps(d, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _sha256_hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _parent_body_for_hmac(parent: ParentRef) -> dict[str, Any]:
    """Body of a parent ref minus the ``hmac`` field itself."""
    body = asdict(parent)
    body["hmac"] = ""
    return body


def _child_body_for_hmac(body: ChildBody) -> dict[str, Any]:
    """Body of a child entry minus the ``hmac`` field itself."""
    d = asdict(body)
    d["hmac"] = ""
    return d


def _compute_hmac(key: bytes, body_sans_hmac: dict[str, Any]) -> str:
    canonical = _canonicalise(body_sans_hmac)
    return _hmac.new(key, canonical, hashlib.sha256).hexdigest()


def compute_child_sha(child_body: ChildBody) -> str:
    """Sha-256 of canonical body bytes (with empty hmac/prev_hmac).

    The first child body line's content-addressed sha. The parent ref
    binds to this via ``child_sha``. We compute over the body with both
    ``hmac`` and ``prev_hmac`` set to empty strings so the sha is
    stable before chain-stamping.
    """
    d = asdict(child_body)
    d["hmac"] = ""
    d["prev_hmac"] = ""
    return "sha256:" + _sha256_hex(_canonicalise(d))


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------


def _iter_raw_lines(path: Path) -> Iterator[bytes]:
    """Yield the raw bytes of each non-empty JSONL line in ``path``.

    Verification works on these bytes rather than on re-serialised
    records: the line on disk *is* the record, so anything the JSON
    parser normalises away has to stay visible to ``verify``.
    """
    if not path.exists():
        return
    raw = path.read_bytes()
    if not raw:
        return
    for line in raw.rstrip(b"\n").split(b"\n"):
        if not line:
            continue
        yield line


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[int]:
    path.parent.mkdir(parents=True, exist_ok=True)
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


# ---------------------------------------------------------------------------
# Verification result
# ---------------------------------------------------------------------------


def _empty_failures() -> list[str]:
    return []


# Exceptions a hostile or corrupt line can raise while being decoded into a
# record. ``verify`` turns every one of them into a reported failure: an
# auditor needs a verdict, not a traceback.
_DECODE_ERRORS = (json.JSONDecodeError, UnicodeDecodeError, KeyError, TypeError, ValueError)


@dataclass(frozen=True, slots=True)
class VerifyResult:
    """Outcome of ``LineageV2Store.verify``."""

    ok: bool
    failures: list[str] = field(default_factory=_empty_failures)
    parent_count: int = 0
    child_count: int = 0


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class LineageV2Store:
    """Two-layer (parent timeline + detached children) lineage store.

    Default location: ``<root>`` is typically ``.sdd/lineage/v2/``. The
    store is safe across threads and processes - state-modifying ops
    take ``flock(LOCK_EX)`` over the parent log.
    """

    def __init__(self, root: Path, *, hmac_key: bytes | None = None) -> None:
        self.root: Path = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / _CHILDREN_DIR).mkdir(parents=True, exist_ok=True)
        self._hmac_key: bytes = hmac_key if hmac_key is not None else _DEFAULT_HMAC_KEY

    # -- paths --------------------------------------------------------------

    @property
    def parent_log(self) -> Path:
        return self.root / _PARENT_LOG_NAME

    def child_log(self, child_sha: str) -> Path:
        """Path for the child body chain identified by ``child_sha``.

        ``child_sha`` may be supplied with or without the ``sha256:``
        prefix. The on-disk filename uses the hex digest only.
        """
        hex_part = child_sha.removeprefix("sha256:")
        return self.root / _CHILDREN_DIR / f"{hex_part}.jsonl"

    # -- internal chain helpers --------------------------------------------

    def _last_parent_hmac(self) -> str:
        """HMAC of the latest parent ref, or empty if the log is fresh."""
        if not self.parent_log.exists():
            return ""
        raw = self.parent_log.read_bytes()
        if not raw.strip():
            return ""
        last = raw.rstrip(b"\n").rsplit(b"\n", 1)[-1]
        try:
            payload = json.loads(last)
        except json.JSONDecodeError:
            return ""
        h = payload.get("hmac", "")
        return str(h) if isinstance(h, str) else ""

    def _last_child_hmac(self, child_path: Path) -> str:
        if not child_path.exists():
            return ""
        raw = child_path.read_bytes()
        if not raw.strip():
            return ""
        last = raw.rstrip(b"\n").rsplit(b"\n", 1)[-1]
        try:
            payload = json.loads(last)
        except json.JSONDecodeError:
            return ""
        h = payload.get("hmac", "")
        return str(h) if isinstance(h, str) else ""

    def _stamp_parent(self, draft: ParentRef) -> ParentRef:
        """Compute and attach the chain hmac to a draft parent ref."""
        body = _parent_body_for_hmac(draft)
        h = _compute_hmac(self._hmac_key, body)
        return ParentRef(
            v=draft.v,
            task_id=draft.task_id,
            child_run_id=draft.child_run_id,
            parent_call_id=draft.parent_call_id,
            summary=draft.summary,
            child_sha=draft.child_sha,
            ts_ns=draft.ts_ns,
            prev_hmac=draft.prev_hmac,
            hmac=h,
        )

    def _stamp_child(self, draft: ChildBody) -> ChildBody:
        """Compute and attach the chain hmac to a draft child body."""
        body = _child_body_for_hmac(draft)
        h = _compute_hmac(self._hmac_key, body)
        return ChildBody(
            v=draft.v,
            task_id=draft.task_id,
            child_run_id=draft.child_run_id,
            seq=draft.seq,
            kind=draft.kind,
            payload=draft.payload.copy(),
            ts_ns=draft.ts_ns,
            prev_hmac=draft.prev_hmac,
            hmac=h,
        )

    # -- public API ---------------------------------------------------------

    def append(self, parent_ref: ParentRef, child_body: ChildBody) -> tuple[str, str]:
        """Atomically write a child body + parent ref pair.

        Both arguments are interpreted as *drafts*: the caller fills in
        the semantic fields and the store stamps ``prev_hmac`` /
        ``hmac`` / ``child_sha`` correctly. Returns ``(child_sha, parent_hmac)``.

        Ordering: child body is written and fsynced first, then the
        parent ref. If a crash hits between the two, ``verify()``
        flags the orphan child but the parent log stays consistent.
        """
        with _exclusive_lock(self.parent_log):
            # 1. Compute the content-address that joins both layers.
            #    The caller may have left child_sha empty - we recompute it
            #    from the canonical body anyway, so the value cannot drift.
            seed_child = ChildBody(
                v=child_body.v,
                task_id=child_body.task_id,
                child_run_id=child_body.child_run_id,
                seq=child_body.seq,
                kind=child_body.kind,
                payload=child_body.payload.copy(),
                ts_ns=child_body.ts_ns,
                prev_hmac="",
                hmac="",
            )
            child_sha = compute_child_sha(seed_child)
            child_path = self.child_log(child_sha)

            # 2. Chain the child body from the prior body in this file
            #    (if any). For a brand-new child file the prev_hmac is "".
            prev_child_hmac = self._last_child_hmac(child_path)
            draft_child = ChildBody(
                v=child_body.v,
                task_id=child_body.task_id,
                child_run_id=child_body.child_run_id,
                seq=child_body.seq,
                kind=child_body.kind,
                payload=child_body.payload.copy(),
                ts_ns=child_body.ts_ns,
                prev_hmac=prev_child_hmac,
                hmac="",
            )
            stamped_child = self._stamp_child(draft_child)

            # 3. Write the child body line first (fsync).
            child_path.parent.mkdir(parents=True, exist_ok=True)
            child_canonical = _canonicalise(asdict(stamped_child))
            with child_path.open("ab") as cfh:
                cfh.write(child_canonical + b"\n")
                cfh.flush()
                os.fsync(cfh.fileno())

            # 4. Chain the parent ref from the prior parent line.
            prev_parent_hmac = self._last_parent_hmac()
            draft_parent = ParentRef(
                v=parent_ref.v,
                task_id=parent_ref.task_id,
                child_run_id=parent_ref.child_run_id,
                parent_call_id=parent_ref.parent_call_id,
                summary=parent_ref.summary,
                child_sha=child_sha,
                ts_ns=parent_ref.ts_ns,
                prev_hmac=prev_parent_hmac,
                hmac="",
            )
            stamped_parent = self._stamp_parent(draft_parent)
            parent_canonical = _canonicalise(asdict(stamped_parent))
            with self.parent_log.open("ab") as pfh:
                pfh.write(parent_canonical + b"\n")
                pfh.flush()
                os.fsync(pfh.fileno())

            return child_sha, stamped_parent.hmac

    def append_child_body(self, child_sha: str, body: ChildBody) -> str:
        """Append an additional body line to an existing child file.

        The parent timeline is untouched. Useful for child runs that
        emit multiple events (start, progress, terminal). Returns the
        new body's hmac.
        """
        child_path = self.child_log(child_sha)
        with _exclusive_lock(self.parent_log):
            if not child_path.exists():
                raise FileNotFoundError(f"child file missing for {child_sha!r}")
            prev = self._last_child_hmac(child_path)
            draft = ChildBody(
                v=body.v,
                task_id=body.task_id,
                child_run_id=body.child_run_id,
                seq=body.seq,
                kind=body.kind,
                payload=body.payload.copy(),
                ts_ns=body.ts_ns,
                prev_hmac=prev,
                hmac="",
            )
            stamped = self._stamp_child(draft)
            canonical = _canonicalise(asdict(stamped))
            with child_path.open("ab") as cfh:
                cfh.write(canonical + b"\n")
                cfh.flush()
                os.fsync(cfh.fileno())
            return stamped.hmac

    # -- read paths ---------------------------------------------------------

    def iter_parent_refs(self) -> Iterator[ParentRef]:
        """Yield every parent ref in append order."""
        if not self.parent_log.exists():
            return
        raw = self.parent_log.read_bytes()
        for line in raw.rstrip(b"\n").split(b"\n"):
            if not line:
                continue
            payload = json.loads(line)
            yield _parent_ref_from_dict(payload)

    def iter_child_bodies(self, child_sha: str) -> Iterator[ChildBody]:
        """Yield every body line in the child file identified by ``child_sha``."""
        path = self.child_log(child_sha)
        if not path.exists():
            return
        raw = path.read_bytes()
        for line in raw.rstrip(b"\n").split(b"\n"):
            if not line:
                continue
            payload = json.loads(line)
            yield _child_body_from_dict(payload)

    def replay(self, task_id: str) -> list[tuple[ParentRef, list[ChildBody]]]:
        """Reconstruct the full timeline for ``task_id``.

        Returns parent-ref order, each paired with the list of child
        bodies attached to that ref (the first body is the
        content-addressed one whose sha matches ``parent_ref.child_sha``).
        Refs whose detached child file is missing surface with an empty
        body list - they are NOT silently dropped, so ``verify`` can
        still flag the orphan.
        """
        result: list[tuple[ParentRef, list[ChildBody]]] = []
        for ref in self.iter_parent_refs():
            if ref.task_id != task_id:
                continue
            bodies = list(self.iter_child_bodies(ref.child_sha))
            result.append((ref, bodies))
        return result

    def verify(self) -> VerifyResult:
        """Validate the HMAC chains across both layers.

        Checks:

        1. Each parent ref's ``hmac`` matches HMAC(body sans hmac).
        2. Each parent ref's ``prev_hmac`` matches the previous line's hmac.
        3. Each child body's ``hmac`` matches HMAC(body sans hmac).
        4. Each child body's ``prev_hmac`` chains from the previous body
           in the same file (or "" for the first).
        5. The first body in each referenced child file has a
           content-address that equals the parent ref's ``child_sha``.
        6. No orphan child files (i.e., a child file with no parent ref
           pointing at it).
        7. Every line's bytes are exactly the canonical serialisation of
           the record it decodes to.

        Check 7 is what makes the byte coverage total. Checks 1-6 run on
        a record *parsed out of* the line, and that parse is not
        injective: unknown keys are dropped and a missing ``payload``
        reads back as ``{}``. A mutation that lands in a region the
        parser normalises away therefore reconstructs the very bytes
        that were signed, and checks 1-6 all pass on it. Comparing the
        line against the canonical form of what it decoded to closes
        that class: every writer in this module emits canonical bytes,
        so a line that is not its own canonical form was not written
        here.

        ``verify`` is total - a malformed, truncated or undecodable line
        is reported as a failure rather than raised. Callers get a
        verdict on every input.
        """
        failures: list[str] = []
        parent_count = 0
        child_count = 0
        referenced_shas: set[str] = set()

        # 1+2+7: parent chain
        prev = ""
        for idx, raw_line in enumerate(_iter_raw_lines(self.parent_log)):
            parent_count += 1
            try:
                ref = _parent_ref_from_dict(json.loads(raw_line))
            except _DECODE_ERRORS as exc:
                failures.append(f"parent[{idx}] undecodable record ({type(exc).__name__})")
                continue
            if _canonicalise(asdict(ref)) != raw_line:
                failures.append(f"parent[{idx}] non-canonical bytes (task={ref.task_id})")
            expected = _compute_hmac(self._hmac_key, _parent_body_for_hmac(ref))
            if ref.hmac != expected:
                failures.append(f"parent[{idx}] hmac mismatch (task={ref.task_id})")
            if ref.prev_hmac != prev:
                failures.append(f"parent[{idx}] prev_hmac break (task={ref.task_id})")
            prev = ref.hmac
            referenced_shas.add(ref.child_sha)

            # 3+4+5+7: child chain for this ref
            child_path = self.child_log(ref.child_sha)
            if not child_path.exists():
                failures.append(f"parent[{idx}] missing child file {ref.child_sha}")
                continue

            child_prev = ""
            for bidx, child_raw in enumerate(_iter_raw_lines(child_path)):
                child_count += 1
                try:
                    body = _child_body_from_dict(json.loads(child_raw))
                except _DECODE_ERRORS as exc:
                    failures.append(f"child[{ref.child_sha[:16]}..][{bidx}] undecodable record ({type(exc).__name__})")
                    continue
                if _canonicalise(asdict(body)) != child_raw:
                    failures.append(f"child[{ref.child_sha[:16]}..][{bidx}] non-canonical bytes")
                if bidx == 0:
                    # Content-address check: the first body's
                    # sha-of-canonical-body must equal child_sha.
                    seed = ChildBody(
                        v=body.v,
                        task_id=body.task_id,
                        child_run_id=body.child_run_id,
                        seq=body.seq,
                        kind=body.kind,
                        payload=body.payload.copy(),
                        ts_ns=body.ts_ns,
                        prev_hmac="",
                        hmac="",
                    )
                    actual_sha = compute_child_sha(seed)
                    if actual_sha != ref.child_sha:
                        failures.append(f"parent[{idx}] child_sha mismatch (expected {ref.child_sha} got {actual_sha})")

                expected_body_hmac = _compute_hmac(self._hmac_key, _child_body_for_hmac(body))
                if body.hmac != expected_body_hmac:
                    failures.append(f"child[{ref.child_sha[:16]}..][{bidx}] hmac mismatch")
                if body.prev_hmac != child_prev:
                    failures.append(f"child[{ref.child_sha[:16]}..][{bidx}] prev_hmac break")
                child_prev = body.hmac

        # 6: orphan child files
        children_dir = self.root / _CHILDREN_DIR
        if children_dir.exists():
            for path in sorted(children_dir.iterdir()):
                if not path.is_file() or path.suffix != ".jsonl":
                    continue
                sha = "sha256:" + path.stem
                if sha not in referenced_shas:
                    failures.append(f"orphan child file {sha}")

        return VerifyResult(
            ok=not failures,
            failures=failures,
            parent_count=parent_count,
            child_count=child_count,
        )

    # -- export -------------------------------------------------------------

    def export_jsonl(self, task_id: str) -> str:
        """Export the full timeline for ``task_id`` as JSONL text.

        Each line is either a parent ref or a child body. Order is:
        parent ref first, then its body lines in file order. Two
        helper marker fields (``_kind: "parent"|"child"``) let
        downstream tooling re-thread the stream without re-loading the
        store.
        """
        out: list[str] = []
        for ref, bodies in self.replay(task_id):
            ref_payload: dict[str, Any] = {
                "_kind": "parent",
            } | asdict(ref)
            out.append(json.dumps(ref_payload, separators=(",", ":"), sort_keys=True))
            for body in bodies:
                body_payload: dict[str, Any] = {
                    "_kind": "child",
                } | asdict(body)
                out.append(json.dumps(body_payload, separators=(",", ":"), sort_keys=True))
        return "\n".join(out) + ("\n" if out else "")

    def export_sigstore(self, task_id: str) -> list[dict[str, Any]]:
        """SLSA v0.3 provenance attestations, one per child body.

        Returns a JSON-serialisable list. Each element follows the
        in-toto Statement envelope wrapping an SLSA Provenance v0.3
        predicate. ``subject.digest.sha256`` is the parent ref's
        ``child_sha`` (minus prefix); the parent's
        ``parent_call_id`` becomes ``invocation.parameters.parent_call_id``.
        """
        attestations: list[dict[str, Any]] = []
        for ref, bodies in self.replay(task_id):
            sha_hex = ref.child_sha.removeprefix("sha256:")
            predicate = {
                "buildDefinition": {
                    "buildType": "https://bernstein.dev/lineage/v2",
                    "externalParameters": {
                        "task_id": ref.task_id,
                        "child_run_id": ref.child_run_id,
                        "parent_call_id": ref.parent_call_id,
                        "summary": ref.summary,
                    },
                    "internalParameters": {
                        "lineage_version": LINEAGE_V2_ENTRY_VERSION,
                    },
                    "resolvedDependencies": [
                        {"name": "parent_ref.prev_hmac", "digest": {"sha256": ref.prev_hmac or "0" * 64}},
                    ],
                },
                "runDetails": {
                    "builder": {"id": "https://bernstein.dev/runners/lineage-v2"},
                    "metadata": {
                        "invocationId": ref.child_run_id,
                        "startedOn": _ns_to_iso(ref.ts_ns),
                    },
                    "byproducts": [
                        {
                            "name": f"child_body[{idx}]",
                            "digest": {"sha256": _sha256_hex(_canonicalise(asdict(b)))},
                        }
                        for idx, b in enumerate(bodies)
                    ],
                },
            }
            attestation = {
                "_type": "https://in-toto.io/Statement/v1",
                "subject": [
                    {
                        "name": f"bernstein-lineage-v2/{ref.task_id}/{ref.child_run_id}",
                        "digest": {"sha256": sha_hex},
                    },
                ],
                "predicateType": "https://slsa.dev/provenance/v0.3",
                "predicate": predicate,
            }
            attestations.append(attestation)
        return attestations


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _ns_to_iso(ts_ns: int) -> str:
    """Return RFC 3339 UTC timestamp for a ns-since-epoch value."""
    import datetime as _dt

    if ts_ns <= 0:
        return "1970-01-01T00:00:00Z"
    return _dt.datetime.fromtimestamp(ts_ns / 1_000_000_000, tz=_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parent_ref_from_dict(payload: dict[str, Any]) -> ParentRef:
    return ParentRef(
        v=int(payload["v"]),
        task_id=str(payload["task_id"]),
        child_run_id=str(payload["child_run_id"]),
        parent_call_id=str(payload["parent_call_id"]),
        summary=str(payload["summary"]),
        child_sha=str(payload["child_sha"]),
        ts_ns=int(payload["ts_ns"]),
        prev_hmac=str(payload["prev_hmac"]),
        hmac=str(payload["hmac"]),
    )


def _child_body_from_dict(payload: dict[str, Any]) -> ChildBody:
    raw_payload: object = payload.get("payload", {})
    payload_dict: dict[str, Any] = {}
    if isinstance(raw_payload, dict):
        raw_dict = cast("dict[Any, Any]", raw_payload)
        for k, v in raw_dict.items():
            payload_dict[str(k)] = v
    return ChildBody(
        v=int(payload["v"]),
        task_id=str(payload["task_id"]),
        child_run_id=str(payload["child_run_id"]),
        seq=int(payload["seq"]),
        kind=str(payload["kind"]),
        payload=payload_dict,
        ts_ns=int(payload["ts_ns"]),
        prev_hmac=str(payload["prev_hmac"]),
        hmac=str(payload["hmac"]),
    )


def is_v2_enabled(env: dict[str, str] | None = None, cfg: dict[str, Any] | None = None) -> bool:
    """Return True if v2 lineage writer should be active.

    Resolution order:

    1. ``BERNSTEIN_LINEAGE_V2`` env var truthy ("1", "true", "yes").
    2. ``cfg["lineage"]["version"] == 2`` from a parsed bernstein.yaml.

    Defaults to False (v1 stays the default).
    """
    e: dict[str, str] = dict(env) if env is not None else os.environ.copy()
    flag = e.get("BERNSTEIN_LINEAGE_V2", "").strip().lower()
    if flag in {"1", "true", "yes", "on"}:
        return True
    if cfg is not None:
        lineage_cfg = cfg.get("lineage")
        if isinstance(lineage_cfg, dict):
            cfg_dict = cast("dict[Any, Any]", lineage_cfg)
            version_val: object = cfg_dict.get("version", 1)
            try:
                if int(cast("Any", version_val)) == 2:
                    return True
            except (TypeError, ValueError):
                return False
    return False


__all__ = [
    "LINEAGE_V2_ENTRY_VERSION",
    "ChildBody",
    "LineageV2Store",
    "ParentRef",
    "VerifyResult",
    "compute_child_sha",
    "is_v2_enabled",
]
