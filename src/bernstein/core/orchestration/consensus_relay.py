"""Cross-cycle consensus relay document for the evolution loop.

When an operator stops Bernstein between two evolution cycles and
restarts ``bernstein run`` from a cold cache, the manager-role agent
otherwise has no way to know what the previous cycle decided, what is
still blocked, or which single action the operator wants picked up
first. Without that breadcrumb the manager spends the first
post-restart tick re-deriving context that was already produced, and
in pathological cases re-issues a decision the prior cycle already
took.

The relay artefact closes that gap. It is a small, append-chained JSON
document keyed by cycle id that records:

* ``phase``         - which phase the prior cycle ended in
  (e.g. ``research``, ``plan``, ``implement``)
* ``last_updated``  - wall clock when the relay was rotated, ns
* ``did_this_cycle``- prose summary of completed work
* ``decisions``     - structured decisions taken this cycle
* ``open_questions``- still-open follow-ups carried forward
* ``next_action``   - the single highest-priority operator follow-up
* ``calibration``   - small kv block for prompt-budget tuning notes
* ``prev_hash``     - HMAC chain pointer to the previous cycle entry

Persistence
-----------
The current cycle is written to ``.sdd/runtime/consensus/<cycle>.json``
via :func:`bernstein.core.persistence.atomic_write.write_atomic_json`,
so a crash between temp-write and rename leaves the previous relay
intact. The directory itself sits inside the ignored ``.sdd/runtime/``
boundary; nothing under it ever lands in git.

HMAC chain
----------
Each entry computes ``operator_hmac`` over the canonicalised body with
the ``operator_hmac`` field blanked. The body also contains
``prev_hash``, which is the HMAC of the immediately previous cycle
entry (or ``"sha256:0"`` for the genesis cycle). The chain is therefore
verifiable end-to-end: any tamper with an intermediate entry breaks
every downstream HMAC.

The key is loaded from ``BERNSTEIN_RELAY_KEY`` (32 hex bytes). If the
env var is missing a deterministic, repo-local key derived from the
operator-id is used, so test runs and air-gapped boxes still get a
chain that round-trips.

CLI / hooks
-----------
The companion CLI lives in ``bernstein.cli.commands.consensus_cmd`` and
exposes ``bernstein consensus show|export|next``. A lifecycle hook
``relay.rotated`` fires whenever :meth:`RelayStore.append` writes a new
entry so plugins (blast-radius scorer, criterion-profile gate) can
observe rotations.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import logging
import os
import re
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, cast

from bernstein.core.dataclass_helpers import typed_replace as _typed_replace
from bernstein.core.persistence.atomic_write import write_atomic_json

__all__ = [
    "DEFAULT_RELAY_DIR",
    "GENESIS_PREV_HASH",
    "MANAGER_RELAY_SECTION",
    "RELAY_VERSION",
    "RelayChainError",
    "RelayDecision",
    "RelayDocument",
    "RelayError",
    "RelayNotFoundError",
    "RelayStore",
    "RelayValidationError",
    "build_spawn_section",
    "canonicalise_relay",
    "compute_relay_hmac",
    "default_relay_key",
    "load_relay_key",
    "spawn_section_for_workdir",
    "verify_chain",
]


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RELAY_VERSION: Final[int] = 1
"""Schema version for the relay document. Bump on shape changes."""

GENESIS_PREV_HASH: Final[str] = "sha256:0"
"""Sentinel ``prev_hash`` for the first cycle in a chain."""

DEFAULT_RELAY_DIR: Final[Path] = Path(".sdd/runtime/consensus")
"""Default on-disk root for per-cycle relay JSON files."""

_CYCLE_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
"""Whitelist for ``cycle_id`` values. Restricts path traversal vectors."""

_PHASE_VALUES: Final[frozenset[str]] = frozenset(
    {
        "research",
        "plan",
        "implement",
        "review",
        "verify",
        "release",
        "idle",
    }
)
"""Recognised phase identifiers. Mirrors phase_pipeline.Phase."""

_MAX_TEXT_LEN: Final[int] = 8000
_MAX_LIST_LEN: Final[int] = 200
_MAX_NEXT_ACTION_LEN: Final[int] = 2000


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RelayError(Exception):
    """Base class for relay errors."""


class RelayValidationError(RelayError, ValueError):
    """Raised when a relay document violates the schema."""


class RelayChainError(RelayError):
    """Raised when HMAC-chain verification fails."""


class RelayNotFoundError(RelayError, FileNotFoundError):
    """Raised when an expected cycle relay is absent."""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RelayDecision:
    """A single decision recorded in a relay document.

    The shape mirrors what the decision log emits, but is intentionally
    embedded so the relay round-trips without needing the decision log
    file to be present.
    """

    title: str
    rationale: str
    confidence: float

    def __post_init__(self) -> None:
        # Runtime callers (JSON loads) sometimes provide off-shape values;
        # the static type is preserved by the dataclass annotation but we
        # still re-check at the boundary.
        if not self.title:
            raise RelayValidationError("decision.title must be a non-empty string")
        if len(self.title) > _MAX_TEXT_LEN:
            raise RelayValidationError("decision.title exceeds max length")
        if len(self.rationale) > _MAX_TEXT_LEN:
            raise RelayValidationError("decision.rationale exceeds max length")
        confidence: object = self.confidence
        if not isinstance(confidence, (int, float)):  # type: ignore[unreachable]
            raise RelayValidationError("decision.confidence must be numeric")
        if not (0.0 <= self.confidence <= 1.0):
            raise RelayValidationError("decision.confidence must be in [0, 1]")

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "rationale": self.rationale,
            "confidence": self.confidence,
        }


@dataclass(frozen=True, slots=True)
class RelayDocument:
    """A single per-cycle relay record.

    Frozen + slots so the canonical byte form is stable. Use
    :meth:`with_next` etc. to derive an updated copy.
    """

    v: int
    cycle_id: str
    prev_cycle_id: str | None
    prev_hash: str
    phase: str
    last_updated_ns: int
    did_this_cycle: str
    decisions: tuple[RelayDecision, ...]
    open_questions: tuple[str, ...]
    blockers: tuple[str, ...]
    next_action: str
    calibration: Mapping[str, str] = field(default_factory=dict[str, str])
    lineage_child: str | None = None
    acknowledged: bool = False
    operator_hmac: str = ""

    def __post_init__(self) -> None:
        _validate_document_fields(self)

    # ------------------------------------------------------------------
    # Serialisation helpers
    # ------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-ready dict in canonical key order."""
        return {
            "v": self.v,
            "cycle_id": self.cycle_id,
            "prev_cycle_id": self.prev_cycle_id,
            "prev_hash": self.prev_hash,
            "phase": self.phase,
            "last_updated_ns": self.last_updated_ns,
            "did_this_cycle": self.did_this_cycle,
            "decisions": [d.to_dict() for d in self.decisions],
            "open_questions": list(self.open_questions),
            "blockers": list(self.blockers),
            "next_action": self.next_action,
            "calibration": dict(self.calibration),
            "lineage_child": self.lineage_child,
            "acknowledged": self.acknowledged,
            "operator_hmac": self.operator_hmac,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> RelayDocument:
        """Reconstruct a :class:`RelayDocument` from ``payload``.

        Raises :class:`RelayValidationError` on shape problems.
        """
        try:
            decisions_raw = payload.get("decisions", [])
            if not isinstance(decisions_raw, list):
                raise RelayValidationError("decisions must be a list")
            decisions_list: list[RelayDecision] = []
            for d_any in cast(list[Any], decisions_raw):
                if not isinstance(d_any, Mapping):
                    raise RelayValidationError("decision entry must be a mapping")
                d = cast(Mapping[str, Any], d_any)
                decisions_list.append(
                    RelayDecision(
                        title=str(d.get("title", "")),
                        rationale=str(d.get("rationale", "")),
                        confidence=cast(float, d.get("confidence", 0.0)),
                    )
                )
            calibration_raw = payload.get("calibration", {})
            if not isinstance(calibration_raw, Mapping):
                raise RelayValidationError("calibration must be a mapping")
            calibration_map = cast(Mapping[str, Any], calibration_raw)
            calibration: dict[str, str] = {k: str(calibration_map[k]) for k in calibration_map}
            open_questions_raw = payload.get("open_questions", [])
            blockers_raw = payload.get("blockers", [])
            if not isinstance(open_questions_raw, list):
                raise RelayValidationError("open_questions must be a list")
            if not isinstance(blockers_raw, list):
                raise RelayValidationError("blockers must be a list")
            prev_cycle_id_raw = payload.get("prev_cycle_id")
            lineage_child_raw = payload.get("lineage_child")
            return cls(
                v=cast(int, payload.get("v", RELAY_VERSION)),
                cycle_id=str(payload["cycle_id"]),
                prev_cycle_id=(None if prev_cycle_id_raw in (None, "") else str(prev_cycle_id_raw)),
                prev_hash=str(payload.get("prev_hash", GENESIS_PREV_HASH)),
                phase=str(payload["phase"]),
                last_updated_ns=cast(int, payload["last_updated_ns"]),
                did_this_cycle=str(payload.get("did_this_cycle", "")),
                decisions=tuple(decisions_list),
                open_questions=tuple(str(x) for x in cast(list[Any], open_questions_raw)),
                blockers=tuple(str(x) for x in cast(list[Any], blockers_raw)),
                next_action=str(payload.get("next_action", "")),
                calibration=calibration,
                lineage_child=(None if lineage_child_raw in (None, "") else str(lineage_child_raw)),
                acknowledged=bool(payload.get("acknowledged", False)),
                operator_hmac=str(payload.get("operator_hmac", "")),
            )
        except KeyError as exc:
            raise RelayValidationError(f"relay payload missing required field: {exc.args[0]!r}") from exc
        except RelayValidationError:
            raise
        except (TypeError, ValueError) as exc:
            raise RelayValidationError(f"relay payload malformed: {exc}") from exc

    # ------------------------------------------------------------------
    # Update helpers
    # ------------------------------------------------------------------
    def acknowledge(self) -> RelayDocument:
        """Return a copy with ``acknowledged=True``."""
        updated = _typed_replace(self, acknowledged=True)
        return updated

    def with_next(self, next_action: str) -> RelayDocument:
        """Return a copy with a new ``next_action``.

        The HMAC is cleared because the body changes; callers should
        re-sign before persisting.
        """
        updated = _typed_replace(self, next_action=next_action, operator_hmac="")
        return updated


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_document_fields(doc: RelayDocument) -> None:
    if doc.v != RELAY_VERSION:
        raise RelayValidationError(f"unsupported relay version: {doc.v}")
    if not _CYCLE_ID_RE.match(doc.cycle_id):
        raise RelayValidationError(f"cycle_id is not safe: {doc.cycle_id!r}")
    if doc.prev_cycle_id is not None and not _CYCLE_ID_RE.match(doc.prev_cycle_id):
        raise RelayValidationError(f"prev_cycle_id is not safe: {doc.prev_cycle_id!r}")
    if doc.prev_cycle_id is not None and doc.prev_cycle_id == doc.cycle_id:
        raise RelayValidationError("prev_cycle_id must differ from cycle_id")
    if doc.phase not in _PHASE_VALUES:
        raise RelayValidationError(f"unknown phase: {doc.phase!r}")
    if doc.last_updated_ns < 0:
        raise RelayValidationError("last_updated_ns must be non-negative")
    if len(doc.did_this_cycle) > _MAX_TEXT_LEN:
        raise RelayValidationError("did_this_cycle exceeds max length")
    if len(doc.next_action) > _MAX_NEXT_ACTION_LEN:
        raise RelayValidationError("next_action exceeds max length")
    if len(doc.decisions) > _MAX_LIST_LEN:
        raise RelayValidationError("too many decisions")
    if len(doc.open_questions) > _MAX_LIST_LEN:
        raise RelayValidationError("too many open_questions")
    if len(doc.blockers) > _MAX_LIST_LEN:
        raise RelayValidationError("too many blockers")
    for q in doc.open_questions:
        if not q:
            raise RelayValidationError("open_questions entries must be non-empty strings")
        if len(q) > _MAX_TEXT_LEN:
            raise RelayValidationError("open_question entry exceeds max length")
    for b in doc.blockers:
        if not b:
            raise RelayValidationError("blockers entries must be non-empty strings")
        if len(b) > _MAX_TEXT_LEN:
            raise RelayValidationError("blocker entry exceeds max length")
    if not doc.prev_hash.startswith("sha256:"):
        raise RelayValidationError("prev_hash must use the sha256: prefix")
    if len(doc.calibration) > _MAX_LIST_LEN:
        raise RelayValidationError("calibration too large")


# ---------------------------------------------------------------------------
# Canonicalisation + HMAC
# ---------------------------------------------------------------------------


def canonicalise_relay(doc: RelayDocument) -> bytes:
    """Return the RFC-8785-style canonical bytes of ``doc``.

    The ``operator_hmac`` field is blanked before encoding so HMAC
    verification is self-consistent: signers and verifiers agree on the
    exact byte string that was signed.
    """
    body = doc.to_dict()
    body["operator_hmac"] = ""
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def compute_relay_hmac(doc: RelayDocument, key: bytes) -> str:
    """Compute the canonical HMAC-SHA256 of ``doc`` under ``key``.

    Raises:
        TypeError: when called from untyped code with a non-bytes key.
        ValueError: when ``key`` is empty.
    """
    # The static signature says bytes, but the function is on the public
    # API boundary so we still defend at runtime.
    key_obj: object = key
    if not isinstance(key_obj, (bytes, bytearray)):  # type: ignore[unreachable]
        raise TypeError("HMAC key must be bytes")
    if not key:
        raise ValueError("HMAC key must be non-empty")
    return _hmac.new(key, canonicalise_relay(doc), hashlib.sha256).hexdigest()


def _relay_entry_hash(doc: RelayDocument) -> str:
    """Return the chain-pointer hash for a fully signed ``doc``."""
    return "sha256:" + hashlib.sha256(canonicalise_relay(doc)).hexdigest()


# ---------------------------------------------------------------------------
# Key resolution
# ---------------------------------------------------------------------------


_DERIVED_KEY_SALT: Final[bytes] = b"bernstein-consensus-relay/v1"


def default_relay_key() -> bytes:
    """Derive a stable repo-local relay HMAC key.

    The chain only protects against accidental tampering; the operator
    secret in ``BERNSTEIN_RELAY_KEY`` is what provides cryptographic
    binding when set. The derived fallback uses the SDD operator-id
    file when available, so two restarts on the same machine produce
    the same chain key.
    """
    op_id = os.environ.get("BERNSTEIN_OPERATOR_ID")
    if not op_id:
        op_id_path = Path(".sdd/operator-id")
        if op_id_path.exists():
            try:
                op_id = op_id_path.read_text(encoding="utf-8").strip()
            except OSError:
                op_id = ""
    if not op_id:
        op_id = "anonymous-operator"
    return hashlib.sha256(_DERIVED_KEY_SALT + op_id.encode("utf-8")).digest()


def load_relay_key() -> bytes:
    """Resolve the HMAC key for the current process.

    Order of precedence:

    1. ``BERNSTEIN_RELAY_KEY`` -- hex-encoded 32 bytes.
    2. The deterministic fallback from :func:`default_relay_key`.
    """
    env = os.environ.get("BERNSTEIN_RELAY_KEY")
    if env:
        env = env.strip()
        try:
            data = bytes.fromhex(env)
        except ValueError as exc:
            raise RelayValidationError("BERNSTEIN_RELAY_KEY must be valid hex") from exc
        if len(data) < 16:
            raise RelayValidationError("BERNSTEIN_RELAY_KEY must be at least 16 bytes")
        return data
    return default_relay_key()


# ---------------------------------------------------------------------------
# Chain verification
# ---------------------------------------------------------------------------


def verify_chain(entries: Iterable[RelayDocument], key: bytes) -> None:
    """Verify the HMAC chain for an ordered sequence of relay entries.

    Args:
        entries: Sequence in cycle order, oldest first.
        key: HMAC key.

    Raises:
        RelayChainError: When any HMAC or ``prev_hash`` link fails.
    """
    prev_hash = GENESIS_PREV_HASH
    prev_cycle_id: str | None = None
    for entry in entries:
        if entry.prev_hash != prev_hash:
            raise RelayChainError(
                f"prev_hash mismatch at cycle {entry.cycle_id}: expected {prev_hash}, got {entry.prev_hash}"
            )
        expected_prev = prev_cycle_id
        if entry.prev_cycle_id != expected_prev:
            raise RelayChainError(
                f"prev_cycle_id mismatch at cycle {entry.cycle_id}: expected {expected_prev}, got {entry.prev_cycle_id}"
            )
        recomputed = compute_relay_hmac(entry, key)
        if not _hmac.compare_digest(recomputed, entry.operator_hmac):
            raise RelayChainError(f"hmac mismatch at cycle {entry.cycle_id}")
        prev_hash = _relay_entry_hash(entry)
        prev_cycle_id = entry.cycle_id


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class RelayStore:
    """On-disk store for per-cycle relay documents.

    Layout::

        <root>/<cycle>.json   # per-cycle relay document
        <root>/_index.json    # ordered list of cycle ids in the chain

    The index is rewritten atomically alongside each append so a crash
    cannot leave the directory listing and the chain out of sync.
    """

    INDEX_NAME: Final[str] = "_index.json"

    def __init__(self, root: Path | str | None = None, *, key: bytes | None = None) -> None:
        if root is None:
            env = os.environ.get("BERNSTEIN_ORCHESTRATION_RELAY_PATH")
            root_path = Path(env) if env else DEFAULT_RELAY_DIR
        else:
            root_path = Path(root)
        self._root = root_path
        self._key = key if key is not None else load_relay_key()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------
    @property
    def root(self) -> Path:
        return self._root

    @property
    def key(self) -> bytes:
        return self._key

    # ------------------------------------------------------------------
    # Filesystem helpers
    # ------------------------------------------------------------------
    def _ensure_root(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)

    def _index_path(self) -> Path:
        return self._root / self.INDEX_NAME

    def _entry_path(self, cycle_id: str) -> Path:
        if not _CYCLE_ID_RE.match(cycle_id):
            raise RelayValidationError(f"cycle_id is not safe: {cycle_id!r}")
        return self._root / f"{cycle_id}.json"

    # ------------------------------------------------------------------
    # Index helpers
    # ------------------------------------------------------------------
    def _read_index(self) -> list[str]:
        path = self._index_path()
        if not path.exists():
            return []
        try:
            data: object = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RelayValidationError(f"relay index unreadable: {exc}") from exc
        if not isinstance(data, list):
            raise RelayValidationError("relay index must be a list")
        return [str(x) for x in cast(list[Any], data)]

    def _write_index(self, ordered_cycles: list[str]) -> None:
        write_atomic_json(self._index_path(), ordered_cycles)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def cycles(self) -> list[str]:
        """Return cycle ids in chain order."""
        return self._read_index()

    def head(self) -> RelayDocument | None:
        """Return the most recently appended relay document, or ``None``."""
        cycles = self.cycles()
        if not cycles:
            return None
        return self.read(cycles[-1])

    def exists(self, cycle_id: str) -> bool:
        return self._entry_path(cycle_id).exists()

    def read(self, cycle_id: str) -> RelayDocument:
        """Read one cycle relay; raise :class:`RelayNotFoundError` if missing."""
        path = self._entry_path(cycle_id)
        if not path.exists():
            raise RelayNotFoundError(f"no relay for cycle {cycle_id!r}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RelayValidationError(f"relay file {path} unreadable: {exc}") from exc
        return RelayDocument.from_dict(payload)

    def all_entries(self) -> list[RelayDocument]:
        """Read every cycle relay in chain order."""
        return [self.read(c) for c in self.cycles()]

    def verify(self) -> None:
        """Verify the full chain on disk."""
        verify_chain(self.all_entries(), self._key)

    def append(
        self,
        *,
        cycle_id: str,
        phase: str,
        did_this_cycle: str = "",
        decisions: Iterable[RelayDecision] | None = None,
        open_questions: Iterable[str] = (),
        blockers: Iterable[str] = (),
        next_action: str = "",
        calibration: Mapping[str, str] | None = None,
        lineage_child: str | None = None,
        now_ns: int | None = None,
    ) -> RelayDocument:
        """Append a new relay document for ``cycle_id``.

        The previous-cycle hash is read from the on-disk chain head.
        Writes are atomic; on success the on-disk chain is
        consistent and verifies under the current key.
        """
        if not _CYCLE_ID_RE.match(cycle_id):
            raise RelayValidationError(f"cycle_id is not safe: {cycle_id!r}")
        self._ensure_root()
        existing = self._read_index()
        if cycle_id in existing:
            raise RelayValidationError(f"cycle {cycle_id!r} already has a relay entry")
        prev_cycle_id: str | None = existing[-1] if existing else None
        if prev_cycle_id is not None:
            prev_doc = self.read(prev_cycle_id)
            prev_hash = _relay_entry_hash(prev_doc)
        else:
            prev_hash = GENESIS_PREV_HASH
        unsigned = RelayDocument(
            v=RELAY_VERSION,
            cycle_id=cycle_id,
            prev_cycle_id=prev_cycle_id,
            prev_hash=prev_hash,
            phase=phase,
            last_updated_ns=time.time_ns() if now_ns is None else int(now_ns),
            did_this_cycle=did_this_cycle,
            decisions=tuple(decisions or ()),
            open_questions=tuple(open_questions),
            blockers=tuple(blockers),
            next_action=next_action,
            calibration=dict(calibration or {}),
            lineage_child=lineage_child,
            acknowledged=False,
            operator_hmac="",
        )
        signed = _typed_replace(unsigned, operator_hmac=compute_relay_hmac(unsigned, self._key))
        write_atomic_json(self._entry_path(cycle_id), signed.to_dict())
        new_index = [*existing, cycle_id]
        self._write_index(new_index)
        _fire_rotation_hook(self._root, signed)
        return signed

    def acknowledge(self, cycle_id: str) -> RelayDocument:
        """Mark a relay as acknowledged by the next cycle.

        This re-signs the entry, so the chain is intentionally
        broken for any downstream cycle that was appended before
        the acknowledgement landed. In practice acknowledgement is
        the first thing the next cycle does on start, so no other
        entries exist yet.
        """
        if not _CYCLE_ID_RE.match(cycle_id):
            raise RelayValidationError(f"cycle_id is not safe: {cycle_id!r}")
        doc = self.read(cycle_id)
        if doc.acknowledged:
            return doc
        updated = _typed_replace(doc, acknowledged=True, operator_hmac="")
        signed = _typed_replace(updated, operator_hmac=compute_relay_hmac(updated, self._key))
        write_atomic_json(self._entry_path(cycle_id), signed.to_dict())
        return signed

    def export_markdown(self, cycle_id: str | None = None) -> str:
        """Render a relay entry as compact operator-friendly markdown."""
        doc = self.head() if cycle_id is None else self.read(cycle_id)
        if doc is None:
            return "_no relay entries yet_"
        lines: list[str] = [
            f"# Cycle relay {doc.cycle_id}",
            "",
            f"- phase: {doc.phase}",
            f"- last_updated_ns: {doc.last_updated_ns}",
            f"- acknowledged: {str(doc.acknowledged).lower()}",
            "",
            "## Did this cycle",
            "",
            doc.did_this_cycle or "_nothing recorded_",
            "",
            "## Decisions",
            "",
        ]
        if doc.decisions:
            for d in doc.decisions:
                lines.append(f"- {d.title} (confidence {d.confidence:.2f})")
        else:
            lines.append("_none_")
        lines.extend(["", "## Open questions", ""])
        if doc.open_questions:
            lines.extend(f"- {q}" for q in doc.open_questions)
        else:
            lines.append("_none_")
        lines.extend(["", "## Blockers", ""])
        if doc.blockers:
            lines.extend(f"- {b}" for b in doc.blockers)
        else:
            lines.append("_none_")
        lines.extend(["", "## Next action", "", doc.next_action or "_unset_", ""])
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Spawn-prompt section builder (issue #4678)
# ---------------------------------------------------------------------------

_SPAWN_SECTION_CAP = 4_000
"""Maximum rendered bytes for the consensus section in a manager spawn prompt."""

MANAGER_RELAY_SECTION = "consensus_relay"
"""Section name both spawn paths must use, so one prompt does not differ from the other."""


def spawn_section_for_workdir(workdir: Path) -> str:
    """Return the manager relay section for ``workdir``, or ``""`` if there is none.

    The two spawn paths resolve the same store and must not disagree about
    where it lives or what the section is called, so the resolution lives
    here rather than being spelled out at each call site.
    """
    return build_spawn_section(relay_root=workdir / ".sdd" / "runtime" / "consensus")


def build_spawn_section(*, relay_root: Path | None = None, key: bytes | None = None) -> str:
    """Render the latest relay entry as a bounded spawn-prompt section.

    For manager-role spawns, this injects the prior cycle's decisions so
    the new manager does not re-litigate settled questions.  The section
    contains phase, decisions, open questions, and next action.

    The section is HMAC-verified before inclusion.  If the chain is broken
    the section is omitted and a debug notice is logged — never inject
    content that does not verify.

    The rendered output is size-capped at :data:`_SPAWN_SECTION_CAP` bytes.
    When the cap is exceeded the section is truncated to fit.

    Args:
        relay_root: Override the relay store root path.
            Defaults to the on-disk consensus directory.
        key: Override the HMAC verification key.  When absent the key is
            resolved through :func:`load_relay_key`.

    Returns:
        The consensus section string, or ``""`` when the store is empty,
        unreadable, or chain verification fails.
    """
    try:
        store = RelayStore(root=relay_root, key=key)
        doc = store.head()
    except (OSError, json.JSONDecodeError, RelayValidationError) as exc:
        log.debug("build_spawn_section: relay store unreadable: %s", exc)
        return ""
    if doc is None:
        return ""

    # Verify the on-disk chain before trusting the content.
    # ChainError is broad enough to cover both HMAC failures and
    # prev_hash/link mismatches.
    try:
        store.verify()
    except RelayChainError as exc:
        log.debug("build_spawn_section: chain verification failed, omitting section: %s", exc)
        return ""

    # Render the decision-carrying fields only.
    # ``export_markdown`` includes last_updated_ns and acknowledged which
    # are non-deterministic and not useful in a spawn-prompt context.
    lines: list[str] = [
        "## Prior cycle consensus",
        "",
        f"- phase: {doc.phase}",
        "",
        "### Decisions",
        "",
    ]
    if doc.decisions:
        for d in doc.decisions:
            lines.append(f"- **{d.title}** (confidence {d.confidence:.2f})")
            if d.rationale:
                lines.append(f"  - {d.rationale}")
    else:
        lines.append("_none_")

    lines.extend(["", "### Open questions", ""])
    if doc.open_questions:
        for q in doc.open_questions:
            lines.append(f"- {q}")
    else:
        lines.append("_none_")

    lines.extend(["", "### Next action", "", doc.next_action or "_unset_"])

    rendered = "\n".join(lines) + "\n"

    if len(rendered.encode("utf-8")) > _SPAWN_SECTION_CAP:
        # Truncate to the byte cap, keeping valid UTF-8.
        rendered_bytes = rendered.encode("utf-8")
        truncated_bytes = rendered_bytes[:_SPAWN_SECTION_CAP]
        try:
            rendered = truncated_bytes.decode("utf-8")
        except UnicodeDecodeError:
            rendered = truncated_bytes[: _SPAWN_SECTION_CAP - 3].decode("utf-8", errors="replace") + "..."

    return rendered


# ---------------------------------------------------------------------------
# Lifecycle hook (best-effort)
# ---------------------------------------------------------------------------


def _fire_rotation_hook(root: Path, doc: RelayDocument) -> None:
    """Best-effort lifecycle hook fan-out.

    The orchestration loop wires a full hook registry; in standalone
    usage (tests, CLI ``export``) we still want the rotation observable
    via the file system, so we always drop a JSONL event next to the
    relay store. The pluggy bridge in the loop can then mirror that
    event into the broader pipeline.
    """
    try:
        events_path = root / "events.jsonl"
        record = {
            "event": "relay.rotated",
            "cycle_id": doc.cycle_id,
            "prev_cycle_id": doc.prev_cycle_id,
            "phase": doc.phase,
            "ts_ns": doc.last_updated_ns,
        }
        with events_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError as exc:
        log.debug("relay rotation event drop failed at %s: %s", root, exc)
