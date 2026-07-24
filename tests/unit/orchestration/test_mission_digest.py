"""Signed daily progress digest tests (#2510).

The digest is a pure deterministic projection of the mission state at a fire
instant, anchored in the HMAC audit chain and posted to chat as its verbatim
projection. Each test maps to an acceptance criterion from the issue:

* Determinism -- two hosts holding byte-identical ledgers, folding at the same
  fire time, derive byte-identical digest bytes, the same digest hash, and the
  exact receipt id (golden test).
* Verifiability -- the posted chat message embeds the digest hash; verification
  detects an edited or truncated message as a mismatch.
* Idempotency -- delivery is idempotent on the digest receipt id; a restart
  between fire computation and delivery does not double-post, and a missed fire
  recomputes to the identical digest after restart.
* Cross-driver -- delivery works on all three shipped chat drivers through the
  common bridge protocol.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

from bernstein.core.chat.bridge import BridgeProtocol
from bernstein.core.evidence.bundle import (
    EvidenceProducer,
    ProducerOutcome,
    build_evidence_bundle,
    load_or_create_evidence_identity,
)
from bernstein.core.orchestration.mission_digest import (
    build_mission_digest,
    record_digest_receipt,
    render_digest_message,
    verify_message_matches,
)
from bernstein.core.orchestration.mission_digest_delivery import (
    DigestDeliveryLedger,
    deliver_digest,
    run_digest_fire,
)
from bernstein.core.orchestration.missions import (
    MissionSpec,
    PhaseSpec,
    define_mission,
    enter_phase,
    gather_evidence_hashes,
    halt_phase,
    mission_ledger_dir,
    pass_phase,
    project_mission_from_ledger,
)
from bernstein.core.persistence.work_ledger import WorkLedger
from bernstein.core.security.audit_chain import EVENT_MISSION_DIGEST_RECEIPT, AuditChainStore

if TYPE_CHECKING:
    from pathlib import Path

_KEY = b"0" * 32
_FIRE = 1_700_000_000


# ---------------------------------------------------------------------------
# Fixtures: build a real two-phase mission ledger with sealed evidence
# ---------------------------------------------------------------------------


def _spec() -> MissionSpec:
    return MissionSpec(
        mission_id="m-1",
        goal="ship the multi-day migration",
        phases=(
            PhaseSpec(phase_id="p1", name="prepare", gate=("task-a",), envelope="mission-m-1-p1", budget_usd=40.0),
            PhaseSpec(phase_id="p2", name="migrate", gate=("task-b",), envelope="mission-m-1-p2", budget_usd=25.0),
        ),
    )


def _seal_evidence(workdir: Path, task_id: str, *, timestamp: int = 1000) -> str:
    priv, pub = load_or_create_evidence_identity(workdir / ".sdd" / "identity")
    outcome = ProducerOutcome(
        producer=EvidenceProducer(name="tests", kind="test", command=("run",), required=True),
        exit_code=0,
        output=f"ok {task_id}\n".encode(),
    )
    bundle = build_evidence_bundle(
        workdir=workdir,
        lineage_root=workdir / ".sdd" / "lineage",
        hmac_key=_KEY,
        private_key_pem=priv,
        public_key_pem=pub,
        task_id=task_id,
        outcomes=(outcome,),
        timestamp=timestamp,
    )
    return bundle.bundle_hash()


def _build_full_mission(sdd_dir: Path, workdir: Path) -> MissionSpec:
    spec = _spec()
    ledger = WorkLedger.open(mission_ledger_dir(sdd_dir, spec.mission_id))
    define_mission(ledger=ledger, spec=spec)
    _seal_evidence(workdir, "task-a")
    enter_phase(ledger=ledger, mission_id=spec.mission_id, phase_id="p1")
    ev = gather_evidence_hashes(workdir, ("task-a",))
    pass_phase(ledger=ledger, spec=spec, phase_id="p1", evidence_hashes=ev, spend_usd=12.0)
    _seal_evidence(workdir, "task-b")
    enter_phase(ledger=ledger, mission_id=spec.mission_id, phase_id="p2")
    ev = gather_evidence_hashes(workdir, ("task-b",))
    pass_phase(ledger=ledger, spec=spec, phase_id="p2", evidence_hashes=ev, spend_usd=9.0)
    ledger.close()
    return spec


def _build_halted_mission(sdd_dir: Path, workdir: Path) -> MissionSpec:
    """Phase 1 passes; phase 2 is halted (envelope exhausted) -> a blocker."""
    spec = _spec()
    ledger = WorkLedger.open(mission_ledger_dir(sdd_dir, spec.mission_id))
    define_mission(ledger=ledger, spec=spec)
    _seal_evidence(workdir, "task-a")
    enter_phase(ledger=ledger, mission_id=spec.mission_id, phase_id="p1")
    ev = gather_evidence_hashes(workdir, ("task-a",))
    pass_phase(ledger=ledger, spec=spec, phase_id="p1", evidence_hashes=ev, spend_usd=12.0)
    enter_phase(ledger=ledger, mission_id=spec.mission_id, phase_id="p2")
    halt_phase(ledger=ledger, spec=spec, phase_id="p2", spend_usd=25.0, reason="envelope_exhausted")
    ledger.close()
    return spec


# ---------------------------------------------------------------------------
# Spy bridge + fake drivers
# ---------------------------------------------------------------------------


class _SpyBridge(BridgeProtocol):
    """Minimal in-memory bridge that records every posted message."""

    platform = "spy"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self._counter = 0

    async def start(self) -> None:  # pragma: no cover - unused
        return None

    async def stop(self) -> None:  # pragma: no cover - unused
        return None

    async def send_message(self, thread_id: str, text: str) -> str:
        self._counter += 1
        self.sent.append((thread_id, text))
        return f"msg-{self._counter}"

    async def edit_message(self, thread_id: str, message_id: str, text: str) -> None:  # pragma: no cover - unused
        return None

    async def push_approval(self, approval: Any) -> str:  # pragma: no cover - unused
        return "approval-1"

    def on_command(self, name: str, handler: Any) -> None:  # pragma: no cover - unused
        return None

    def on_button(self, handler: Any) -> None:  # pragma: no cover - unused
        return None


# ---------------------------------------------------------------------------
# Determinism (AC: byte-identical digest across hosts + golden hash)
# ---------------------------------------------------------------------------


def test_two_hosts_compute_identical_digest_bytes(tmp_path: Path) -> None:
    host_a, host_b = tmp_path / "a", tmp_path / "b"
    host_a.mkdir()
    host_b.mkdir()
    _build_full_mission(host_a / ".sdd", host_a)
    _build_full_mission(host_b / ".sdd", host_b)

    proj_a = project_mission_from_ledger(sdd_dir=host_a / ".sdd", workdir=host_a, mission_id="m-1")
    proj_b = project_mission_from_ledger(sdd_dir=host_b / ".sdd", workdir=host_b, mission_id="m-1")
    digest_a = build_mission_digest(proj_a, fire_time=_FIRE)
    digest_b = build_mission_digest(proj_b, fire_time=_FIRE)

    assert digest_a.canonical_bytes() == digest_b.canonical_bytes()
    assert digest_a.digest_hash() == digest_b.digest_hash()
    assert digest_a.receipt_id() == digest_b.receipt_id()


def test_digest_hash_is_pinned_for_the_canonical_fixture(tmp_path: Path) -> None:
    """Golden: a fixed ledger + fire time produces a pinned digest hash."""
    _build_full_mission(tmp_path / ".sdd", tmp_path)
    proj = project_mission_from_ledger(sdd_dir=tmp_path / ".sdd", workdir=tmp_path, mission_id="m-1")
    digest = build_mission_digest(proj, fire_time=_FIRE)

    assert digest.overall == "complete"
    assert digest.phases_passed == 2
    assert digest.gates_passed == 2
    assert digest.total_spend_usd == pytest.approx(21.0)
    # Pinned digest hash + receipt id. Regenerate deliberately if the digest
    # schema changes; a silent drift means the digest is no longer byte-stable.
    assert digest.digest_hash() == _PINNED_DIGEST_HASH
    assert digest.receipt_id() == _PINNED_RECEIPT_ID


#: Pinned digest hash / receipt id for the canonical fixture at ``_FIRE``. The
#: digest embeds ``mission_status_hash``, so the #2683 status-projection bump to
#: v2 moved these even though the digest schema itself is unchanged.
_PINNED_DIGEST_HASH = "453baccccb0df0db95b45965499416bc7cb136f463a0dd519df999e8bc3b0d61"
_PINNED_RECEIPT_ID = "missiondigest-4a6f4f2bfdf666ca93c2875d"


def test_fire_time_must_be_int(tmp_path: Path) -> None:
    _build_full_mission(tmp_path / ".sdd", tmp_path)
    proj = project_mission_from_ledger(sdd_dir=tmp_path / ".sdd", workdir=tmp_path, mission_id="m-1")
    with pytest.raises(TypeError, match="integer epoch"):
        build_mission_digest(proj, fire_time=float(_FIRE))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="integer epoch"):
        build_mission_digest(proj, fire_time=True)  # type: ignore[arg-type]


def test_envelope_spend_and_blockers_for_halted_mission(tmp_path: Path) -> None:
    _build_halted_mission(tmp_path / ".sdd", tmp_path)
    proj = project_mission_from_ledger(sdd_dir=tmp_path / ".sdd", workdir=tmp_path, mission_id="m-1")
    digest = build_mission_digest(proj, fire_time=_FIRE)

    assert digest.overall == "halted"
    assert digest.gates_failed == 1
    assert [b.phase_id for b in digest.blockers] == ["p2"]
    assert digest.blockers[0].reason == "halted"
    envs = {row.envelope: row.spend_usd for row in digest.envelope_spend}
    assert envs == {"mission-m-1-p1": pytest.approx(12.0), "mission-m-1-p2": pytest.approx(25.0)}


# ---------------------------------------------------------------------------
# Verifiability (AC: posted message embeds hash; edit/truncation detected)
# ---------------------------------------------------------------------------


def test_message_embeds_hashes_and_verifies(tmp_path: Path) -> None:
    _build_full_mission(tmp_path / ".sdd", tmp_path)
    proj = project_mission_from_ledger(sdd_dir=tmp_path / ".sdd", workdir=tmp_path, mission_id="m-1")
    digest = build_mission_digest(proj, fire_time=_FIRE)
    text = render_digest_message(digest)

    assert digest.digest_hash() in text
    assert digest.mission_status_hash in text
    assert digest.receipt_id() in text
    result = verify_message_matches(text, digest)
    assert result.matches is True
    assert result.reason == "ok"


def test_edited_message_is_detected_as_mismatch(tmp_path: Path) -> None:
    _build_full_mission(tmp_path / ".sdd", tmp_path)
    proj = project_mission_from_ledger(sdd_dir=tmp_path / ".sdd", workdir=tmp_path, mission_id="m-1")
    digest = build_mission_digest(proj, fire_time=_FIRE)
    text = render_digest_message(digest)

    # An edit that keeps the length but flips a spend figure.
    edited = text.replace("$21.00", "$99.00")
    assert edited != text
    result = verify_message_matches(edited, digest)
    assert result.matches is False


def test_truncated_message_is_detected_as_mismatch(tmp_path: Path) -> None:
    _build_full_mission(tmp_path / ".sdd", tmp_path)
    proj = project_mission_from_ledger(sdd_dir=tmp_path / ".sdd", workdir=tmp_path, mission_id="m-1")
    digest = build_mission_digest(proj, fire_time=_FIRE)
    text = render_digest_message(digest)

    truncated = text[: len(text) // 2]
    result = verify_message_matches(truncated, digest)
    assert result.matches is False
    assert "truncated" in result.reason


# ---------------------------------------------------------------------------
# Chain anchoring (AC: receipt recorded + chain verifies)
# ---------------------------------------------------------------------------


def test_digest_receipt_records_and_chain_verifies(tmp_path: Path) -> None:
    _build_full_mission(tmp_path / ".sdd", tmp_path)
    proj = project_mission_from_ledger(sdd_dir=tmp_path / ".sdd", workdir=tmp_path, mission_id="m-1")
    digest = build_mission_digest(proj, fire_time=_FIRE)
    chain = AuditChainStore(tmp_path / "audit")

    event = record_digest_receipt(chain, digest, schedule_id="mission-digest:m-1")
    assert event.event_type == EVENT_MISSION_DIGEST_RECEIPT
    assert event.details["digest_hash"] == digest.digest_hash()
    assert event.details["receipt_id"] == digest.receipt_id()
    assert event.details["mission_status_hash"] == digest.mission_status_hash
    assert "prev_chain_digest" in event.details

    ok, errors = chain.verify()
    assert ok, errors


# ---------------------------------------------------------------------------
# Idempotent delivery + restart (AC: no double-post; missed fire recomputes)
# ---------------------------------------------------------------------------


def test_delivery_is_idempotent_on_receipt_id(tmp_path: Path) -> None:
    sdd_dir = tmp_path / ".sdd"
    _build_full_mission(sdd_dir, tmp_path)
    proj = project_mission_from_ledger(sdd_dir=sdd_dir, workdir=tmp_path, mission_id="m-1")
    digest = build_mission_digest(proj, fire_time=_FIRE)
    bridge = _SpyBridge()
    ledger = DigestDeliveryLedger(sdd_dir, "m-1")

    first = asyncio.run(deliver_digest(bridge=bridge, thread_id="chan", digest=digest, ledger=ledger))
    second = asyncio.run(deliver_digest(bridge=bridge, thread_id="chan", digest=digest, ledger=ledger))

    assert first.posted is True
    assert second.posted is False
    assert second.reason == "already_delivered"
    assert len(bridge.sent) == 1  # exactly one post


def test_restart_does_not_double_post(tmp_path: Path) -> None:
    """A fresh delivery ledger over the same file still sees the prior post."""
    sdd_dir = tmp_path / ".sdd"
    _build_full_mission(sdd_dir, tmp_path)
    proj = project_mission_from_ledger(sdd_dir=sdd_dir, workdir=tmp_path, mission_id="m-1")
    digest = build_mission_digest(proj, fire_time=_FIRE)
    bridge = _SpyBridge()

    asyncio.run(
        deliver_digest(bridge=bridge, thread_id="chan", digest=digest, ledger=DigestDeliveryLedger(sdd_dir, "m-1"))
    )
    # Simulate a restart: brand-new ledger instance reads the durable file.
    outcome = asyncio.run(
        deliver_digest(bridge=bridge, thread_id="chan", digest=digest, ledger=DigestDeliveryLedger(sdd_dir, "m-1"))
    )
    assert outcome.posted is False
    assert len(bridge.sent) == 1


def test_run_digest_fire_is_idempotent_and_records_one_receipt(tmp_path: Path) -> None:
    sdd_dir = tmp_path / ".sdd"
    _build_full_mission(sdd_dir, tmp_path)
    chain = AuditChainStore(tmp_path / "audit")
    bridge = _SpyBridge()

    def _fire() -> Any:
        return asyncio.run(
            run_digest_fire(
                sdd_dir=sdd_dir,
                workdir=tmp_path,
                mission_id="m-1",
                fire_time=_FIRE,
                chain=chain,
                bridge=bridge,
                thread_id="chan",
            )
        )

    first = _fire()
    second = _fire()

    assert first.outcome.posted is True
    assert first.recorded_receipt is True
    assert second.outcome.posted is False
    assert second.recorded_receipt is False
    assert len(bridge.sent) == 1
    # Exactly one digest receipt in the chain despite two fires.
    receipts = chain.query(event_type=EVENT_MISSION_DIGEST_RECEIPT)
    assert len(receipts) == 1
    assert receipts[0].details["fire_graph_hash"] == first.fire_graph_hash
    ok, errors = chain.verify()
    assert ok, errors


def test_missed_fire_recomputes_to_identical_digest(tmp_path: Path) -> None:
    """A fire computed, then recomputed after 'restart', is byte-identical."""
    sdd_dir = tmp_path / ".sdd"
    _build_full_mission(sdd_dir, tmp_path)

    proj1 = project_mission_from_ledger(sdd_dir=sdd_dir, workdir=tmp_path, mission_id="m-1")
    digest1 = build_mission_digest(proj1, fire_time=_FIRE)
    # Restart: reproject from the same on-disk ledger and rebuild the digest.
    proj2 = project_mission_from_ledger(sdd_dir=sdd_dir, workdir=tmp_path, mission_id="m-1")
    digest2 = build_mission_digest(proj2, fire_time=_FIRE)

    assert digest1.canonical_bytes() == digest2.canonical_bytes()
    assert digest1.receipt_id() == digest2.receipt_id()


# ---------------------------------------------------------------------------
# Cross-driver delivery (AC: all three shipped drivers via common bridge)
# ---------------------------------------------------------------------------


def _make_slack(monkeypatch: pytest.MonkeyPatch, captured: list[str]) -> BridgeProtocol:
    from bernstein.core.chat.drivers.slack import SlackBridge

    bridge = SlackBridge(token="xoxb-test", app_token="xapp-test")

    class _FakeWeb:
        async def chat_postMessage(self, *, channel: str, text: str, metadata: Any) -> dict[str, str]:
            captured.append(text)
            return {"ts": "1.1"}

    monkeypatch.setattr(bridge, "_require_web", lambda: _FakeWeb())
    return bridge


def _make_discord(monkeypatch: pytest.MonkeyPatch, captured: list[str]) -> BridgeProtocol:
    from bernstein.core.chat.drivers.discord import DiscordBridge

    bridge = DiscordBridge(token="discord-test")

    class _FakeChannel:
        async def send(self, text: str) -> Any:
            captured.append(text)
            return type("Sent", (), {"id": 42})()

    async def _resolve(_thread_id: str) -> Any:
        return _FakeChannel()

    monkeypatch.setattr(bridge, "_resolve_channel", _resolve)
    monkeypatch.setattr(bridge, "_build_signed_envelope", lambda _text: {})
    return bridge


def _make_telegram(monkeypatch: pytest.MonkeyPatch, captured: list[str]) -> BridgeProtocol:
    from bernstein.core.chat.drivers.telegram import TelegramBridge

    bridge = TelegramBridge(token="telegram-test")

    class _FakeBot:
        async def send_message(self, *, chat_id: Any, text: str) -> Any:
            captured.append(text)
            return type("Sent", (), {"message_id": 7})()

    class _FakeApp:
        bot = _FakeBot()

    monkeypatch.setattr(bridge, "_require_app", lambda: _FakeApp())
    return bridge


@pytest.mark.parametrize("factory", [_make_slack, _make_discord, _make_telegram])
def test_delivery_works_on_all_three_drivers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    factory: Any,
) -> None:
    sdd_dir = tmp_path / ".sdd"
    _build_full_mission(sdd_dir, tmp_path)
    proj = project_mission_from_ledger(sdd_dir=sdd_dir, workdir=tmp_path, mission_id="m-1")
    digest = build_mission_digest(proj, fire_time=_FIRE)

    captured: list[str] = []
    bridge = factory(monkeypatch, captured)
    ledger = DigestDeliveryLedger(sdd_dir, "m-1")

    outcome = asyncio.run(deliver_digest(bridge=bridge, thread_id="chan", digest=digest, ledger=ledger))

    assert outcome.posted is True
    assert len(captured) == 1
    # The exact digest projection (with the embedded hash) reached the wire.
    assert captured[0] == render_digest_message(digest)
    assert digest.digest_hash() in captured[0]
