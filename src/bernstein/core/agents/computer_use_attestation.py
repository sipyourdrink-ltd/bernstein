"""Per-action anchoring, recording, and replay for computer-use agents (#2606).

This module is the outbound counterpart to
:mod:`bernstein.core.agents.multimodal_attestation`. Where that module attests
an operator-supplied ``--attach`` image *into* a run, this one instruments the
boundary crossing for a *third-party autonomous* browser / computer-use agent so
each action the external agent decides on becomes an anchored, signed, replayable
record.

Substrate coupling (the point, not decoration)
-----------------------------------------------
Each recorded action touches four substrate pieces, and stripping any one
collapses the feature rather than merely losing its log:

* :mod:`bernstein.core.persistence.cas_store` -- the pre-action screenshot bytes
  are stored once by SHA-256 (dedup, byte-exact retrieval on replay).
* :mod:`bernstein.core.lineage.recorder` / ``store`` -- the action anchor is the
  entry's ``content_hash`` and its ``parent_hashes`` is the prior anchor, so the
  action stream is a single-parent, HMAC-enveloped, Ed25519-signed lineage
  chain. The signed head anchor is the run's identity.
* :mod:`bernstein.core.security.audit_chain` -- a ``computer_use.action`` event
  per action is the replay manifest: which CAS blob, which DOM digest, which
  action produced each anchor.

Replay walks the signed chain, re-hashes the stored bytes, recomputes each
anchor, and reports the *first* action index whose recomputed anchor diverges
from the signed value. A one-byte flip in a stored screenshot changes that
action's observation hash, hence its anchor, hence surfaces as a hash mismatch
at the exact index -- never a flaky text assertion. A plain-file implementation
(screenshots on disk with no anchor binding) cannot reproduce this: it has
nothing to recompute the tampered bytes against.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bernstein.core.agents.computer_use import (
    GENESIS_ANCHOR,
    Action,
    ActionKind,
    action_anchor_preimage,
    compute_action_anchor,
    compute_observation_hash,
    is_computer_use_capable,
)
from bernstein.core.agents.multimodal_attestation import CapabilityRefusal
from bernstein.core.security.audit_chain import (
    EVENT_COMPUTER_USE_ACTION,
    record_computer_use_action,
)

if TYPE_CHECKING:
    from bernstein.core.lineage.identity import AgentCard
    from bernstein.core.lineage.recorder import LineageRecorder
    from bernstein.core.lineage.store import LineageStore
    from bernstein.core.persistence.cas_store import CASStore
    from bernstein.core.security.audit_chain import AuditChainStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Capability gating
# ---------------------------------------------------------------------------

#: Adapters that advertise ``is_computer_use_capable() == True``. Sorted tuple
#: so refusal messages are deterministic.
_CAPABLE_SUGGESTIONS: tuple[str, ...] = ("computer_use",)


class ComputerUseRefusal(CapabilityRefusal):
    """Raised when an incapable adapter is asked to front a browser task.

    Subclasses :class:`~bernstein.core.agents.multimodal_attestation.CapabilityRefusal`
    so callers that already catch ``CapabilityRefusal`` (the multimodal boundary
    error) catch this too -- it is the *same structured* refusal, populated with
    ``suggested_adapters``, just for the computer-use boundary.
    """

    def __init__(self, adapter_name: str, suggested_adapters: tuple[str, ...]) -> None:
        # Bypass the multimodal-specific message but keep the same public
        # attributes (adapter_name / suggested_adapters) so isinstance checks
        # and structured handling are identical.
        self.adapter_name = adapter_name
        self.suggested_adapters = suggested_adapters
        RuntimeError.__init__(
            self,
            f"Adapter {adapter_name!r} cannot front a browser / computer-use task. "
            f"Suggested capable adapters: {', '.join(suggested_adapters)}.",
        )


def refuse_when_incapable(
    *,
    adapter_name: str,
    action_count: int,
) -> None:
    """Raise :class:`ComputerUseRefusal` before any process launch.

    Parity with
    :func:`bernstein.core.agents.multimodal_attestation.refuse_when_incapable`:
    an incapable adapter fronting a non-empty browser task is refused up front,
    with ``suggested_adapters`` naming capable adapters.

    Args:
        adapter_name: Registry name of the selected adapter (case-insensitive).
        action_count: Number of browser actions the task will drive. Zero means
            there is nothing to refuse (no browser work requested).

    Raises:
        ComputerUseRefusal: When ``action_count > 0`` and the adapter is not
            computer-use-capable.
    """
    if action_count <= 0:
        return
    if is_computer_use_capable(adapter_name):
        return
    raise ComputerUseRefusal(adapter_name=adapter_name, suggested_adapters=_CAPABLE_SUGGESTIONS)


# ---------------------------------------------------------------------------
# Byte-cap refusal (CAS growth guard)
# ---------------------------------------------------------------------------


class ActionByteCapExceeded(RuntimeError):
    """Raised when a run's cumulative screenshot bytes exceed the per-run cap.

    Per-action screenshots are large; the cap is a typed refusal on overflow so
    a runaway run cannot silently balloon the content-addressed store.
    """

    def __init__(self, run_id: str, cap_bytes: int, would_be_bytes: int) -> None:
        self.run_id = run_id
        self.cap_bytes = cap_bytes
        self.would_be_bytes = would_be_bytes
        super().__init__(
            f"Computer-use run {run_id!r} would store {would_be_bytes} screenshot "
            f"bytes, exceeding the per-run cap of {cap_bytes} bytes."
        )


#: Default per-run cumulative screenshot byte cap (64 MiB). Generous for a
#: normal click flow; a typed refusal fires past it.
DEFAULT_RUN_BYTE_CAP = 64 * 1024 * 1024


# ---------------------------------------------------------------------------
# Recorded action
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActionAnchorRecord:
    """A single recorded, anchored action.

    Attributes:
        index: Zero-based position in the run.
        anchor: The action anchor (hex). Equal to the lineage entry's
            ``content_hash`` minus the ``sha256:`` prefix.
        prev_anchor: The prior anchor folded into this one.
        observation_hash: ``sha256(screenshot_bytes + dom_digest)``.
        screenshot_sha256: CAS key of the pre-action screenshot bytes.
        dom_digest: Normalised DOM/accessibility digest.
        action: The canonicalised action.
        lineage_entry_hash: The signed lineage entry hash (``sha256:...``).
    """

    index: int
    anchor: str
    prev_anchor: str
    observation_hash: str
    screenshot_sha256: str
    dom_digest: str
    action: Action
    lineage_entry_hash: str


def _artefact_path(run_id: str, index: int) -> str:
    """Return the repo-relative lineage artefact path for an action."""
    return f".sdd/computer-use/{run_id}/action-{index:06d}.json"


def _parse_index(artefact_path: str) -> int | None:
    """Parse the action index out of an artefact path, or ``None``."""
    stem = artefact_path.rsplit("/", 1)[-1]
    if not stem.startswith("action-") or not stem.endswith(".json"):
        return None
    digits = stem[len("action-") : -len(".json")]
    return int(digits) if digits.isdigit() else None


# ---------------------------------------------------------------------------
# Recording session
# ---------------------------------------------------------------------------


class ComputerUseSession:
    """Records the per-action lineage chain for one browser / computer-use run.

    The session owns the running ``prev_anchor`` and action index. Each
    :meth:`record_action` stores the pre-action bytes in CAS, records one signed
    lineage entry whose ``content_hash`` is the action anchor and whose
    ``parent_hashes`` is the prior anchor, and appends the ``computer_use.action``
    replay-manifest event to the audit chain.

    Args:
        run_id: Identifier for this run (namespaces the lineage artefact paths).
        worker_id: Bernstein agent id fronting the external agent.
        worktree_id: Worktree the run is isolated to.
        cas: Content-addressed blob store (reused, not re-created).
        audit_chain: Audit chain store.
        lineage_recorder: The lineage recorder (holds the operator HMAC key).
        agent_card: Agent Card carrying the signing public key id.
        private_key_pem: PEM-encoded Ed25519 private key for the agent.
        run_byte_cap: Per-run cumulative screenshot byte cap.
    """

    def __init__(
        self,
        *,
        run_id: str,
        worker_id: str,
        worktree_id: str,
        cas: CASStore,
        audit_chain: AuditChainStore,
        lineage_recorder: LineageRecorder,
        agent_card: AgentCard,
        private_key_pem: str,
        run_byte_cap: int = DEFAULT_RUN_BYTE_CAP,
    ) -> None:
        self.run_id = run_id
        self.worker_id = worker_id
        self.worktree_id = worktree_id
        self._cas = cas
        self._audit_chain = audit_chain
        self._lineage = lineage_recorder
        self._agent_card = agent_card
        self._private_key_pem = private_key_pem
        self._run_byte_cap = run_byte_cap
        self._prev_anchor = GENESIS_ANCHOR
        self._index = 0
        self._bytes_stored = 0
        self._records: list[ActionAnchorRecord] = []

    @property
    def head_anchor(self) -> str:
        """The current head anchor (the run's identity so far)."""
        return self._prev_anchor

    @property
    def records(self) -> tuple[ActionAnchorRecord, ...]:
        """All actions recorded so far, in order."""
        return tuple(self._records)

    def record_action(
        self,
        *,
        screenshot_bytes: bytes,
        dom_digest: str,
        action: Action,
    ) -> ActionAnchorRecord:
        """Store the observation, anchor the action, and sign + audit it.

        Args:
            screenshot_bytes: The exact pre-action screenshot bytes the agent
                saw before it acted.
            dom_digest: Normalised DOM/accessibility digest of the same state.
            action: The canonicalised action the agent chose.

        Returns:
            The :class:`ActionAnchorRecord` for the recorded action.

        Raises:
            ActionByteCapExceeded: When storing the bytes would push the run
                past its cumulative screenshot byte cap.
        """
        would_be = self._bytes_stored + len(screenshot_bytes)
        if would_be > self._run_byte_cap:
            raise ActionByteCapExceeded(
                run_id=self.run_id,
                cap_bytes=self._run_byte_cap,
                would_be_bytes=would_be,
            )

        index = self._index
        prev_anchor = self._prev_anchor

        # 1. Pre-action bytes into CAS (dedup, byte-exact retrieval on replay).
        screenshot_sha256 = self._cas.put(
            screenshot_bytes,
            content_type="image/png",
            metadata={
                "kind": "computer_use.pre_action_screenshot",
                "run_id": self.run_id,
                "worktree_id": self.worktree_id,
                "worker_id": self.worker_id,
                "action_index": index,
            },
        )
        self._bytes_stored = would_be

        # 2. observation_hash binds the anchor to the exact observed bytes.
        observation_hash = compute_observation_hash(screenshot_bytes=screenshot_bytes, dom_digest=dom_digest)

        # 3. The anchor IS the lineage content: content_hash == "sha256:" + anchor.
        anchor = compute_action_anchor(prev_anchor=prev_anchor, observation_hash=observation_hash, action=action)
        preimage = action_anchor_preimage(prev_anchor=prev_anchor, observation_hash=observation_hash, action=action)

        # 4. Signed lineage entry: parent_hashes is the prior anchor (genesis has
        #    none). A fresh artefact path per action keeps the tip empty so the
        #    only parent is the anchor we pass, matching the anchor chain 1:1.
        extra_parents = None if index == 0 else [f"sha256:{prev_anchor}"]
        span_id = hashlib.sha256(f"{self.run_id}:{index}".encode()).hexdigest()[:16]
        entry_hash = self._lineage.record_write(
            artefact_path=_artefact_path(self.run_id, index),
            new_content=preimage,
            agent_id=self.worker_id,
            agent_card=self._agent_card,
            private_key_pem=self._private_key_pem,
            tool_call_id=f"{self.run_id}:action:{index}",
            span_id=span_id,
            artefact_kind="tool-result",
            extra_parents=extra_parents,
        )

        # 5. Replay-manifest event on the audit chain.
        record_computer_use_action(
            chain=self._audit_chain,
            run_id=self.run_id,
            action_index=index,
            anchor=anchor,
            prev_anchor=prev_anchor,
            observation_hash=observation_hash,
            screenshot_sha256=screenshot_sha256,
            dom_digest=dom_digest,
            action_kind=str(action.kind),
            action_target=action.target,
            action_value_digest=action.value_digest,
            lineage_entry_hash=entry_hash,
            worker_id=self.worker_id,
            worktree_id=self.worktree_id,
        )

        record = ActionAnchorRecord(
            index=index,
            anchor=anchor,
            prev_anchor=prev_anchor,
            observation_hash=observation_hash,
            screenshot_sha256=screenshot_sha256,
            dom_digest=dom_digest,
            action=action,
            lineage_entry_hash=entry_hash,
        )
        self._records.append(record)
        self._prev_anchor = anchor
        self._index += 1
        return record


# ---------------------------------------------------------------------------
# Replay + divergence detection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplayDivergence:
    """The first action index whose recomputed anchor diverges from the chain.

    Attributes:
        action_index: The exact action index at which replay diverged.
        expected_anchor: The signed anchor recorded in the lineage chain.
        recomputed_anchor: The anchor recomputed from the stored bytes/action.
        reason: A short machine-readable reason
            (``anchor-mismatch`` / ``missing-manifest`` / ``missing-bytes`` /
            ``missing-lineage``).
    """

    action_index: int
    expected_anchor: str
    recomputed_anchor: str
    reason: str


@dataclass(frozen=True)
class ReplayResult:
    """Outcome of replaying a computer-use run.

    Attributes:
        ok: ``True`` only when every action's recomputed anchor matched the
            signed chain and the parent linkage held.
        action_count: Number of signed actions in the run.
        head_anchor: The recomputed head anchor (the run's identity) when ``ok``.
        divergence: The first divergence, or ``None`` when ``ok``.
    """

    ok: bool
    action_count: int
    head_anchor: str
    divergence: ReplayDivergence | None


def replay_run(
    *,
    run_id: str,
    store: LineageStore,
    audit_chain: AuditChainStore,
    cas: CASStore,
) -> ReplayResult:
    """Replay a completed run and detect the first divergent action index.

    Given only the lineage store, the audit chain, and the CAS -- the same three
    substrate pieces a fresh checkout ships -- this recomputes each action anchor
    by re-hashing the stored pre-action bytes and comparing against the signed
    ``content_hash``. It never re-runs the external agent.

    Args:
        run_id: The run to replay.
        store: The lineage store holding the signed anchor chain.
        audit_chain: The audit chain holding the per-action replay manifest.
        cas: The content-addressed store holding the pre-action screenshots. Read
            with ``verify=False`` so a tampered blob is detected at the anchor
            level (naming the index), not masked as a CAS integrity error.

    Returns:
        A :class:`ReplayResult`. ``ok`` is ``True`` only for a byte-identical
        replay up to the signed head; otherwise ``divergence`` names the exact
        first differing action index and both anchors.
    """
    prefix = f".sdd/computer-use/{run_id}/action-"

    # 1. Signed anchors from the lineage chain, keyed by action index.
    signed: dict[int, tuple[str, list[str]]] = {}
    for entry, _jws in store.read_log():
        if not entry.artefact_path.startswith(prefix):
            continue
        idx = _parse_index(entry.artefact_path)
        if idx is None:
            continue
        anchor_hex = entry.content_hash.split(":", 1)[1] if ":" in entry.content_hash else entry.content_hash
        signed[idx] = (anchor_hex, list(entry.parent_hashes))

    # 2. Replay manifest from the audit chain, keyed by action index.
    manifest: dict[int, dict[str, object]] = {}
    for ev in audit_chain.query(event_type=EVENT_COMPUTER_USE_ACTION):
        details = ev.details
        if details.get("run_id") != run_id:
            continue
        try:
            manifest[int(details["action_index"])] = details  # type: ignore[arg-type]
        except (KeyError, TypeError, ValueError):
            continue

    # 3. Walk indices in order, recomputing each anchor and checking linkage.
    prev = GENESIS_ANCHOR
    head = GENESIS_ANCHOR
    for idx in sorted(signed):
        expected_anchor, parents = signed[idx]
        details = manifest.get(idx)
        if details is None:
            return ReplayResult(
                ok=False,
                action_count=len(signed),
                head_anchor=head,
                divergence=ReplayDivergence(idx, expected_anchor, "", "missing-manifest"),
            )

        screenshot = cas.get(str(details["screenshot_sha256"]), verify=False)
        if screenshot is None:
            return ReplayResult(
                ok=False,
                action_count=len(signed),
                head_anchor=head,
                divergence=ReplayDivergence(idx, expected_anchor, "", "missing-bytes"),
            )

        action = Action(
            kind=ActionKind(str(details["action_kind"])),
            target=str(details["action_target"]),
            value_digest=str(details["action_value_digest"]),
        )
        observation_hash = compute_observation_hash(
            screenshot_bytes=screenshot,
            dom_digest=str(details["dom_digest"]),
        )
        recomputed = compute_action_anchor(prev_anchor=prev, observation_hash=observation_hash, action=action)

        if recomputed != expected_anchor:
            return ReplayResult(
                ok=False,
                action_count=len(signed),
                head_anchor=head,
                divergence=ReplayDivergence(idx, expected_anchor, recomputed, "anchor-mismatch"),
            )

        expected_parents = [] if prev == GENESIS_ANCHOR else [f"sha256:{prev}"]
        if parents != expected_parents:
            return ReplayResult(
                ok=False,
                action_count=len(signed),
                head_anchor=head,
                divergence=ReplayDivergence(idx, expected_anchor, recomputed, "parent-mismatch"),
            )

        prev = recomputed
        head = recomputed

    return ReplayResult(ok=True, action_count=len(signed), head_anchor=head, divergence=None)


__all__ = [
    "DEFAULT_RUN_BYTE_CAP",
    "ActionAnchorRecord",
    "ActionByteCapExceeded",
    "ComputerUseRefusal",
    "ComputerUseSession",
    "ReplayDivergence",
    "ReplayResult",
    "refuse_when_incapable",
    "replay_run",
]
