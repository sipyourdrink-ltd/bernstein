"""Per-action lineage anchoring for browser / computer-use agents (#2606).

Encodes the issue's acceptance criteria as tests:

* Per-action anchoring: each action stores its pre-action screenshot bytes and
  DOM digest in CAS and records one ``action_anchor`` lineage entry whose
  ``parent_hashes`` is the prior anchor.
* Verifiability (empirical SOTA proof): flipping one byte of one stored
  screenshot makes chain-walk replay fail and name the exact action index and
  anchor; a plain-file implementation with no anchor binding cannot satisfy the
  same test.
* Determinism: replay of a completed run recomputes byte-identical anchors up to
  the signed head; a divergent re-run surfaces as a hash mismatch at the first
  differing index, not free text.
* Capability refusal: an incapable adapter fronting a browser task raises the
  structured refusal with populated ``suggested_adapters``, before any launch.
* CAS-growth guard: a per-run byte cap fires a typed refusal.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.agents.computer_use import (
    GENESIS_ANCHOR,
    Action,
    ActionKind,
    action_anchor_preimage,
    compute_action_anchor,
    compute_observation_hash,
    digest_typed_value,
    is_computer_use_capable,
)
from bernstein.core.agents.computer_use_attestation import (
    DEFAULT_RUN_BYTE_CAP,
    ActionByteCapExceeded,
    ComputerUseRefusal,
    ComputerUseSession,
    refuse_when_incapable,
    replay_run,
)
from bernstein.core.agents.multimodal_attestation import CapabilityRefusal
from bernstein.core.lineage.identity import AgentCard, generate_keypair
from bernstein.core.lineage.recorder import LineageRecorder
from bernstein.core.lineage.store import LineageStore
from bernstein.core.persistence.cas_store import CASStore
from bernstein.core.security.audit_chain import (
    EVENT_COMPUTER_USE_ACTION,
    AuditChainStore,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _session(tmp_path: Path, *, run_id: str = "run-1", worktree_id: str = "wt-a") -> ComputerUseSession:
    cas = CASStore(tmp_path / "cas")
    chain = AuditChainStore(audit_dir=tmp_path / "audit", key=b"k" * 32)
    store = LineageStore(tmp_path / "lineage")
    recorder = LineageRecorder(store, operator_hmac_key=b"h" * 32)
    priv, pub = generate_keypair()
    card = AgentCard(agent_id="agent:cu-worker", kid="kid-cu-1", public_key_pem=pub)
    return ComputerUseSession(
        run_id=run_id,
        worker_id="agent:cu-worker",
        worktree_id=worktree_id,
        cas=cas,
        audit_chain=chain,
        lineage_recorder=recorder,
        agent_card=card,
        private_key_pem=priv,
    )


def _drive_three(session: ComputerUseSession) -> None:
    """Record a short golden action transcript: navigate, type, click."""
    session.record_action(
        screenshot_bytes=b"\x89PNG-home-screen",
        dom_digest="dom-home",
        action=Action(kind=ActionKind.NAVIGATE, target="https://example.test/signup"),
    )
    session.record_action(
        screenshot_bytes=b"\x89PNG-form-screen",
        dom_digest="dom-form",
        action=Action(kind=ActionKind.TYPE, target="#email", value_digest=digest_typed_value("user@example.test")),
    )
    session.record_action(
        screenshot_bytes=b"\x89PNG-ready-screen",
        dom_digest="dom-ready",
        action=Action(kind=ActionKind.CLICK, target="#submit"),
    )


# ---------------------------------------------------------------------------
# Anchor primitives
# ---------------------------------------------------------------------------


class TestAnchorPrimitives:
    def test_observation_hash_is_deterministic(self) -> None:
        a = compute_observation_hash(screenshot_bytes=b"abc", dom_digest="d")
        b = compute_observation_hash(screenshot_bytes=b"abc", dom_digest="d")
        assert a == b
        assert len(a) == 64

    def test_observation_hash_changes_with_screenshot_bytes(self) -> None:
        a = compute_observation_hash(screenshot_bytes=b"abc", dom_digest="d")
        b = compute_observation_hash(screenshot_bytes=b"abd", dom_digest="d")
        assert a != b

    def test_observation_hash_changes_with_dom_digest(self) -> None:
        a = compute_observation_hash(screenshot_bytes=b"abc", dom_digest="d1")
        b = compute_observation_hash(screenshot_bytes=b"abc", dom_digest="d2")
        assert a != b

    def test_anchor_folds_in_prev_anchor(self) -> None:
        obs = compute_observation_hash(screenshot_bytes=b"x", dom_digest="d")
        act = Action(kind=ActionKind.CLICK, target="#a")
        first = compute_action_anchor(prev_anchor=GENESIS_ANCHOR, observation_hash=obs, action=act)
        chained = compute_action_anchor(prev_anchor=first, observation_hash=obs, action=act)
        assert first != chained

    def test_anchor_is_deterministic(self) -> None:
        obs = compute_observation_hash(screenshot_bytes=b"x", dom_digest="d")
        act = Action(kind=ActionKind.TYPE, target="#f", value_digest="deadbeef")
        one = compute_action_anchor(prev_anchor="p", observation_hash=obs, action=act)
        two = compute_action_anchor(prev_anchor="p", observation_hash=obs, action=act)
        assert one == two

    def test_typed_value_digest_never_exposes_raw(self) -> None:
        secret = "hunter2-super-secret"
        digest = digest_typed_value(secret)
        assert secret not in digest
        assert len(digest) == 64

    def test_preimage_hashes_to_anchor(self) -> None:
        import hashlib

        obs = compute_observation_hash(screenshot_bytes=b"x", dom_digest="d")
        act = Action(kind=ActionKind.CLICK, target="#a")
        preimage = action_anchor_preimage(prev_anchor="p", observation_hash=obs, action=act)
        anchor = compute_action_anchor(prev_anchor="p", observation_hash=obs, action=act)
        assert hashlib.sha256(preimage).hexdigest() == anchor


# ---------------------------------------------------------------------------
# AC: per-action anchoring stores bytes in CAS + records lineage entry chained
# to the prior anchor
# ---------------------------------------------------------------------------


class TestPerActionAnchoring:
    def test_each_action_stores_screenshot_in_cas(self, tmp_path: Path) -> None:
        session = _session(tmp_path)
        _drive_three(session)
        cas = CASStore(tmp_path / "cas")
        for record in session.records:
            blob = cas.get(record.screenshot_sha256)
            assert blob is not None, f"action {record.index} screenshot missing from CAS"

    def test_lineage_content_hash_equals_anchor(self, tmp_path: Path) -> None:
        session = _session(tmp_path)
        _drive_three(session)
        store = LineageStore(tmp_path / "lineage")
        by_path = {entry.artefact_path: entry for entry, _ in store.read_log()}
        for record in session.records:
            path = f".sdd/computer-use/{session.run_id}/action-{record.index:06d}.json"
            entry = by_path[path]
            assert entry.content_hash == f"sha256:{record.anchor}"

    def test_parent_hashes_is_prior_anchor(self, tmp_path: Path) -> None:
        session = _session(tmp_path)
        _drive_three(session)
        store = LineageStore(tmp_path / "lineage")
        by_path = {entry.artefact_path: entry for entry, _ in store.read_log()}

        recs = session.records
        genesis = by_path[f".sdd/computer-use/{session.run_id}/action-000000.json"]
        assert genesis.parent_hashes == []  # genesis has no parent

        for record in recs[1:]:
            path = f".sdd/computer-use/{session.run_id}/action-{record.index:06d}.json"
            entry = by_path[path]
            assert entry.parent_hashes == [f"sha256:{record.prev_anchor}"]

    def test_action_records_audit_event(self, tmp_path: Path) -> None:
        session = _session(tmp_path)
        _drive_three(session)
        chain = AuditChainStore(audit_dir=tmp_path / "audit", key=b"k" * 32)
        events = chain.query(event_type=EVENT_COMPUTER_USE_ACTION)
        assert len(events) == 3
        indices = sorted(int(e.details["action_index"]) for e in events)
        assert indices == [0, 1, 2]

    def test_audit_chain_verifies_after_actions(self, tmp_path: Path) -> None:
        session = _session(tmp_path)
        _drive_three(session)
        chain = AuditChainStore(audit_dir=tmp_path / "audit", key=b"k" * 32)
        ok, errors = chain.verify()
        assert ok, errors

    def test_head_anchor_is_last_action_anchor(self, tmp_path: Path) -> None:
        session = _session(tmp_path)
        _drive_three(session)
        assert session.head_anchor == session.records[-1].anchor


# ---------------------------------------------------------------------------
# AC: determinism -- replay recomputes byte-identical anchors up to the head
# ---------------------------------------------------------------------------


class TestReplayDeterminism:
    def test_clean_replay_passes(self, tmp_path: Path) -> None:
        session = _session(tmp_path)
        _drive_three(session)
        store = LineageStore(tmp_path / "lineage")
        chain = AuditChainStore(audit_dir=tmp_path / "audit", key=b"k" * 32)
        cas = CASStore(tmp_path / "cas")

        result = replay_run(run_id=session.run_id, store=store, audit_chain=chain, cas=cas)
        assert result.ok
        assert result.divergence is None
        assert result.action_count == 3
        assert result.head_anchor == session.records[-1].anchor

    def test_replay_from_fresh_store_objects(self, tmp_path: Path) -> None:
        # Determinism: a fresh checkout (new store objects over the same dirs)
        # recomputes the identical head anchor without re-running the agent.
        session = _session(tmp_path)
        _drive_three(session)
        expected_head = session.head_anchor

        result = replay_run(
            run_id=session.run_id,
            store=LineageStore(tmp_path / "lineage"),
            audit_chain=AuditChainStore(audit_dir=tmp_path / "audit", key=b"k" * 32),
            cas=CASStore(tmp_path / "cas"),
        )
        assert result.ok
        assert result.head_anchor == expected_head


# ---------------------------------------------------------------------------
# AC: verifiability (empirical SOTA proof) -- one flipped byte fails replay at
# the exact index; a plain-file implementation cannot detect it
# ---------------------------------------------------------------------------


class TestTamperDetection:
    def test_flipping_one_screenshot_byte_fails_at_exact_index(self, tmp_path: Path) -> None:
        session = _session(tmp_path)
        _drive_three(session)
        cas = CASStore(tmp_path / "cas")

        target = session.records[1]  # tamper with the second action's screenshot
        blob_path = cas._blob_path(target.screenshot_sha256)
        raw = bytearray(blob_path.read_bytes())
        raw[0] ^= 0x01  # flip exactly one bit of one byte
        blob_path.write_bytes(bytes(raw))

        result = replay_run(
            run_id=session.run_id,
            store=LineageStore(tmp_path / "lineage"),
            audit_chain=AuditChainStore(audit_dir=tmp_path / "audit", key=b"k" * 32),
            cas=CASStore(tmp_path / "cas"),
        )
        assert not result.ok
        assert result.divergence is not None
        assert result.divergence.action_index == 1  # names the exact index
        assert result.divergence.reason == "anchor-mismatch"
        assert result.divergence.expected_anchor == target.anchor
        assert result.divergence.recomputed_anchor != target.anchor

    def test_tampering_a_recorded_action_field_fails_replay(self, tmp_path: Path) -> None:
        # Flip one field of one recorded action in the audit manifest: replay
        # recomputes a different anchor and diverges at that index.
        session = _session(tmp_path)
        _drive_three(session)

        audit_log = tmp_path / "audit"
        # The audit chain writes JSONL; rewrite the click target on the last
        # action and confirm replay refuses (the manifest no longer reproduces
        # the signed anchor).
        log_files = list(audit_log.glob("*.jsonl"))
        assert log_files, "audit log file missing"
        log_path = log_files[0]
        lines = log_path.read_text().splitlines()
        rewritten: list[str] = []
        for line in lines:
            obj = json.loads(line)
            details = obj.get("details", {})
            if obj.get("event_type") == EVENT_COMPUTER_USE_ACTION and details.get("action_index") == 2:
                details["action_target"] = "#cancel"  # was "#submit"
            rewritten.append(json.dumps(obj))
        log_path.write_text("\n".join(rewritten) + "\n")

        result = replay_run(
            run_id=session.run_id,
            store=LineageStore(tmp_path / "lineage"),
            audit_chain=AuditChainStore(audit_dir=tmp_path / "audit", key=b"k" * 32),
            cas=CASStore(tmp_path / "cas"),
        )
        assert not result.ok
        assert result.divergence is not None
        assert result.divergence.action_index == 2

    def test_plain_file_implementation_cannot_detect_tamper(self, tmp_path: Path) -> None:
        """A plain-file screenshot log with no anchor binding cannot pass the
        same tamper test -- proving the substrate coupling is load-bearing.

        The naive implementation writes each screenshot to a plain file and a
        JSON action log next to it, then "replays" by reading the log. It has no
        content-addressed anchor to recompute, so flipping a byte in a stored
        screenshot is invisible to it: its replay still reports success. The
        lineage-anchored implementation, given the identical tamper, fails at the
        exact index (asserted above).
        """

        class _PlainFileActionLog:
            """Screenshots as plain files + a JSON log; no anchor, no CAS, no sig."""

            def __init__(self, root: Path) -> None:
                self.root = root
                self.root.mkdir(parents=True, exist_ok=True)
                self._entries: list[dict[str, str]] = []

            def record(self, *, index: int, screenshot_bytes: bytes, action: dict[str, str]) -> None:
                shot = self.root / f"action-{index}.png"
                shot.write_bytes(screenshot_bytes)
                self._entries.append({"index": str(index), "screenshot": str(shot), **action})
                (self.root / "log.json").write_text(json.dumps(self._entries))

            def replay_ok(self) -> bool:
                # No anchor binds the bytes to the log, so "replay" is just:
                # does the log parse and do the files exist? Tampering with the
                # file contents cannot be seen here.
                entries = json.loads((self.root / "log.json").read_text())
                return all(Path(e["screenshot"]).exists() for e in entries)

        plain = _PlainFileActionLog(tmp_path / "plain")
        plain.record(index=0, screenshot_bytes=b"shot0", action={"kind": "navigate", "target": "x"})
        plain.record(index=1, screenshot_bytes=b"shot1", action={"kind": "click", "target": "#a"})
        assert plain.replay_ok()

        # Same tamper: flip a byte of a stored screenshot.
        shot = tmp_path / "plain" / "action-1.png"
        raw = bytearray(shot.read_bytes())
        raw[0] ^= 0x01
        shot.write_bytes(bytes(raw))

        # The plain-file implementation is blind to the tamper -- it still passes.
        assert plain.replay_ok(), "plain-file replay should be unable to detect the tamper"


# ---------------------------------------------------------------------------
# AC: capability refusal before any launch
# ---------------------------------------------------------------------------


class TestCapabilityRefusal:
    def test_incapable_adapter_refused(self) -> None:
        with pytest.raises(ComputerUseRefusal) as exc:
            refuse_when_incapable(adapter_name="aider", action_count=2)
        assert exc.value.adapter_name == "aider"
        assert exc.value.suggested_adapters == ("computer_use",)

    def test_refusal_is_a_capability_refusal(self) -> None:
        # It IS the same structured refusal the multimodal boundary raises.
        with pytest.raises(CapabilityRefusal):
            refuse_when_incapable(adapter_name="cursor", action_count=1)

    def test_capable_adapter_not_refused(self) -> None:
        refuse_when_incapable(adapter_name="computer_use", action_count=5)  # no raise

    def test_zero_actions_never_refused(self) -> None:
        refuse_when_incapable(adapter_name="aider", action_count=0)  # nothing to front

    def test_capability_predicate(self) -> None:
        assert is_computer_use_capable("computer_use")
        assert is_computer_use_capable("COMPUTER_USE")  # case-insensitive
        assert not is_computer_use_capable("aider")


# ---------------------------------------------------------------------------
# AC: CAS-growth guard -- per-run byte cap fires a typed refusal
# ---------------------------------------------------------------------------


class TestByteCap:
    def test_default_cap_is_generous(self) -> None:
        assert DEFAULT_RUN_BYTE_CAP >= 1024 * 1024

    def test_byte_cap_fires_typed_refusal(self, tmp_path: Path) -> None:
        session = _session(tmp_path)
        session._run_byte_cap = 10
        with pytest.raises(ActionByteCapExceeded) as exc:
            session.record_action(
                screenshot_bytes=b"this is definitely more than ten bytes",
                dom_digest="d",
                action=Action(kind=ActionKind.SCREENSHOT),
            )
        assert exc.value.run_id == session.run_id
        assert exc.value.cap_bytes == 10
