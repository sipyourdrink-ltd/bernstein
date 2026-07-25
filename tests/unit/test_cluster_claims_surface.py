"""The MESH claim-gossip route and the ``bernstein cluster claims`` CLI (#2558).

Two operator-facing surfaces of the leaderless topology:

* ``POST /cluster/claims/gossip`` -- folds a peer's signed claim receipts only
  after signature and chain link verify, answers ``409`` on a STAR node, and
  reports a fork rather than merging divergent chains.
* ``bernstein cluster claims log | head | verify`` -- reads and replays the
  journal with no live node. ``verify`` prints the head hash and the failing
  entry index on tamper, and exits ``2`` on a fork.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from bernstein.core.models import ClusterConfig, MeshPeerKey
from click.testing import CliRunner
from cryptography.hazmat.primitives import serialization
from fastapi.testclient import TestClient

from bernstein.cli.commands.cluster_cmd import cluster_group
from bernstein.core.orchestration.tracker_pipeline import (
    ClaimJournal,
    default_claim_journal_path,
)
from bernstein.core.protocols.cluster.mesh_coordinator import MeshCoordinator
from bernstein.core.server import create_app
from bernstein.core.tasks.models import ClusterTopology

if TYPE_CHECKING:
    from bernstein.core.orchestration.tracker_pipeline import ClaimReceipt

_SECRET = "cluster-shared-secret-value"  # NOSONAR - test fixture, not a real credential


def _kms(tmp_path: Path, *, seed: int, name: str) -> object:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from bernstein.core.security.lineage_kms import FileBasedKMSAdapter

    key_path = tmp_path / f"{name}.pem"
    if not key_path.exists():
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_bytes(
            Ed25519PrivateKey.from_private_bytes(bytes([seed]) * 32).private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
    return FileBasedKMSAdapter(key_path, kid=name)


def _peer(
    tmp_path: Path,
    *,
    node_id: str = "peer-node",
    seed: int = 7,
    label: str | None = None,
) -> MeshCoordinator:
    """A remote node whose receipts are gossiped into the server under test.

    ``label`` names the peer's own journal file and key material independently
    of the ``node_id`` its receipts declare, so a test can build a node that
    *claims* an identity it does not hold.
    """
    slug = label or node_id
    return MeshCoordinator(
        journal=ClaimJournal(
            tmp_path / f"{slug}.jsonl",
            kms_adapter=_kms(tmp_path, seed=seed, name=f"{slug}-key"),  # type: ignore[arg-type]
            node_id=node_id,
        ),
    )


def _pin(node_id: str, *, seed: int = 7) -> MeshPeerKey:
    """The pin an operator would configure for a peer signing with ``seed``."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from bernstein.core.identity.http_signing import public_key_jwk_from_pem

    public_pem = (
        Ed25519PrivateKey.from_private_bytes(bytes([seed]) * 32)
        .public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return MeshPeerKey(node_id=node_id, public_key_x=public_key_jwk_from_pem(public_pem)["x"])


# ---------------------------------------------------------------------------
# POST /cluster/claims/gossip
# ---------------------------------------------------------------------------


class TestGossipRoute:
    """Verify-before-fold, peer-key pinning, STAR refusal, and fork reporting."""

    def _mesh_client(self, tmp_path: Path, *, pins: tuple[MeshPeerKey, ...] = ()) -> TestClient:
        app = create_app(
            jsonl_path=tmp_path / ".sdd" / "runtime" / "tasks.jsonl",
            auth_token=_SECRET,
            cluster_config=ClusterConfig(
                enabled=True,
                topology=ClusterTopology.MESH,
                auth_token=_SECRET,
                gossip_peer_keys=pins,
            ),
        )
        return TestClient(app)

    def _star_client(self, tmp_path: Path) -> TestClient:
        app = create_app(
            jsonl_path=tmp_path / ".sdd" / "runtime" / "tasks.jsonl",
            auth_token=_SECRET,
            cluster_config=ClusterConfig(enabled=True, auth_token=_SECRET),
        )
        return TestClient(app)

    def _push(self, client: TestClient, receipts: list[ClaimReceipt]) -> dict[str, object]:
        response = client.post(
            "/cluster/claims/gossip",
            json={"receipts": [r.to_dict() for r in receipts], "head": receipts[-1].entry_hash},
            headers={"Authorization": f"Bearer {_SECRET}"},
        )
        assert response.status_code == 200, response.text
        return response.json()  # type: ignore[no-any-return]

    def test_star_node_refuses_gossip(self, tmp_path: Path) -> None:
        """MESH stays opt-in: a STAR node has no journal to fold into."""
        peer = _peer(tmp_path)
        peer.claim(tracker="jira", ticket_id="T-1", role="backend", claimer_id="w", now=1000.0)
        client = self._star_client(tmp_path)
        response = client.post(
            "/cluster/claims/gossip",
            json={"receipts": [r.to_dict() for r in peer.journal.read()]},
            headers={"Authorization": f"Bearer {_SECRET}"},
        )
        assert response.status_code == 409
        assert "mesh" in response.json()["detail"].lower()

    def test_mesh_node_folds_verified_receipts(self, tmp_path: Path) -> None:
        """A signed, chain-extending receipt from a pinned peer is folded."""
        peer = _peer(tmp_path)
        peer.claim(tracker="jira", ticket_id="T-1", role="backend", claimer_id="w-peer", now=1000.0)
        client = self._mesh_client(tmp_path, pins=(_pin("peer-node"),))

        body = self._push(client, peer.journal.read())
        assert body["accepted"] == 1
        assert body["forked"] is False
        assert body["head"] == peer.head()
        assert [r["status"] for r in body["results"]] == ["applied"]  # type: ignore[index,union-attr]

        coordinator = client.app.state.mesh_coordinator  # type: ignore[attr-defined]
        holder = coordinator.state().holder("jira", "T-1", "backend")
        assert holder is not None
        assert holder.claimer_id == "w-peer"

    def test_tampered_receipt_is_rejected_not_folded(self, tmp_path: Path) -> None:
        """The signature and hash are checked before anything is written."""
        peer = _peer(tmp_path)
        peer.claim(tracker="jira", ticket_id="T-1", role="backend", claimer_id="w-peer", now=1000.0)
        client = self._mesh_client(tmp_path, pins=(_pin("peer-node"),))

        wire = peer.journal.read()[0].to_dict()
        wire["claimer_id"] = "attacker"
        response = client.post(
            "/cluster/claims/gossip",
            json={"receipts": [wire]},
            headers={"Authorization": f"Bearer {_SECRET}"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["accepted"] == 0
        assert body["results"][0]["status"] == "rejected"

        coordinator = client.app.state.mesh_coordinator  # type: ignore[attr-defined]
        assert coordinator.state().holder("jira", "T-1", "backend") is None

    def test_divergent_chain_reports_a_fork(self, tmp_path: Path) -> None:
        """A receipt that does not extend the local head forks, never merges."""
        client = self._mesh_client(tmp_path, pins=(_pin("peer-node"),))
        coordinator = client.app.state.mesh_coordinator  # type: ignore[attr-defined]
        # The server already has a local entry, so a peer receipt built on
        # genesis cannot extend it.
        coordinator.claim(tracker="jira", ticket_id="T-local", role="backend", claimer_id="w", now=1000.0)

        peer = _peer(tmp_path)
        peer.claim(tracker="jira", ticket_id="T-remote", role="qa", claimer_id="w-peer", now=1000.0)

        body = self._push(client, peer.journal.read())
        assert body["forked"] is True
        assert body["results"][0]["status"] == "forked"  # type: ignore[index]
        assert body["results"][0]["divergence_index"] == 0  # type: ignore[index]
        # Not merged: the peer's claim did not become a hold.
        assert coordinator.state().holder("jira", "T-remote", "qa") is None
        assert len(coordinator.state().forks) == 1

    def test_gossip_requires_cluster_auth(self, tmp_path: Path) -> None:
        """The route sits behind the same credential a worker joins with."""
        peer = _peer(tmp_path)
        peer.claim(tracker="jira", ticket_id="T-1", role="backend", claimer_id="w", now=1000.0)
        client = self._mesh_client(tmp_path)
        response = client.post(
            "/cluster/claims/gossip",
            json={"receipts": [r.to_dict() for r in peer.journal.read()]},
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert response.status_code == 401

    def test_malformed_receipt_is_a_422(self, tmp_path: Path) -> None:
        """A body that is not a receipt fails validation, not the journal."""
        client = self._mesh_client(tmp_path)
        response = client.post(
            "/cluster/claims/gossip",
            json={"receipts": [{"kind": "claim"}]},
            headers={"Authorization": f"Bearer {_SECRET}"},
        )
        assert response.status_code == 422

    # -- peer-key pinning (#2997) --------------------------------------

    def test_receipt_signed_by_a_key_that_is_not_the_pinned_one_is_rejected(self, tmp_path: Path) -> None:
        """A node_id must be *proved*, not asserted, over the real route.

        The impostor holds a valid cluster token and mints a receipt that is
        internally perfect: correctly chained, correctly hashed, and validly
        Ed25519-signed by the key it holds. The only thing wrong with it is
        that the key is not the one pinned for the ``node_id`` it declares.

        Both receipts travel through ``POST /cluster/claims/gossip`` -- the
        production path, with the coordinator built by ``create_app`` from
        config -- because the defect this covers is the *wiring*: calling the
        coordinator directly would pass even with nothing wired.
        """
        pinned = _pin("peer-node", seed=7)
        client = self._mesh_client(tmp_path, pins=(pinned,))

        impostor = _peer(tmp_path, node_id="peer-node", seed=9, label="impostor")
        impostor.claim(tracker="jira", ticket_id="T-1", role="backend", claimer_id="w-impostor", now=1000.0)
        body = self._push(client, impostor.journal.read())

        assert body["accepted"] == 0
        assert body["results"][0]["status"] == "rejected"  # type: ignore[index]
        assert "signature" in str(body["results"][0]["reason"]).lower()  # type: ignore[index]

        coordinator = client.app.state.mesh_coordinator  # type: ignore[attr-defined]
        # Nothing was written: the journal is not merely un-folded, it is empty.
        assert coordinator.state().holder("jira", "T-1", "backend") is None
        assert coordinator.journal.read() == []

        # The pin admits the real holder of that identity, so the rejection
        # above is the pin working, not gossip being broken outright.
        genuine = _peer(tmp_path, node_id="peer-node", seed=7, label="genuine")
        genuine.claim(tracker="jira", ticket_id="T-1", role="backend", claimer_id="w-peer", now=1000.0)
        accepted = self._push(client, genuine.journal.read())
        assert accepted["accepted"] == 1
        assert coordinator.state().holder("jira", "T-1", "backend") is not None

    def test_a_mesh_node_with_no_pins_folds_nothing(self, tmp_path: Path) -> None:
        """Fail-closed default: an unpinned node_id is refused, not trusted.

        Before #2997 no production path populated the pin map, so a receipt was
        verified against the key it shipped with -- authenticating the bytes but
        not the sender. A MESH node now starts closed.
        """
        peer = _peer(tmp_path)
        peer.claim(tracker="jira", ticket_id="T-1", role="backend", claimer_id="w-peer", now=1000.0)
        client = self._mesh_client(tmp_path)

        body = self._push(client, peer.journal.read())
        assert body["accepted"] == 0
        assert body["results"][0]["status"] == "rejected"  # type: ignore[index]
        assert "no trusted key pinned" in str(body["results"][0]["reason"])  # type: ignore[index]

        coordinator = client.app.state.mesh_coordinator  # type: ignore[attr-defined]
        assert coordinator.state().holder("jira", "T-1", "backend") is None

    def test_a_pinned_peer_cannot_speak_for_another_pinned_peer(self, tmp_path: Path) -> None:
        """Holding one pinned key does not confer another peer's identity."""
        client = self._mesh_client(tmp_path, pins=(_pin("peer-b", seed=7), _pin("peer-c", seed=9)))

        # peer-c's key, signing a receipt that declares peer-b's node_id.
        crossed = _peer(tmp_path, node_id="peer-b", seed=9, label="crossed")
        crossed.claim(tracker="jira", ticket_id="T-1", role="backend", claimer_id="w", now=1000.0)

        body = self._push(client, crossed.journal.read())
        assert body["results"][0]["status"] == "rejected"  # type: ignore[index]
        coordinator = client.app.state.mesh_coordinator  # type: ignore[attr-defined]
        assert coordinator.journal.read() == []

    def test_the_node_pins_its_own_key_to_its_own_node_id(self, tmp_path: Path) -> None:
        """A peer cannot echo back receipts forged in this node's name.

        Self-pinning also keeps recovery working: a node that lost its journal
        can still fold its own history back from a peer, because the receipts
        it wrote verify against the key it still holds.
        """
        client = self._mesh_client(tmp_path)
        coordinator = client.app.state.mesh_coordinator  # type: ignore[attr-defined]

        forger = _peer(tmp_path, node_id=coordinator.node_id, seed=11, label="forger")
        forger.claim(tracker="jira", ticket_id="T-1", role="backend", claimer_id="w", now=1000.0)
        body = self._push(client, forger.journal.read())
        assert body["results"][0]["status"] == "rejected"  # type: ignore[index]

        # This node's own receipt, gossiped back to it, is a duplicate -- it
        # verifies against the self-pin rather than being refused as unknown.
        local = coordinator.claim(tracker="jira", ticket_id="T-2", role="qa", claimer_id="w", now=1000.0)
        assert local.granted is True
        echoed = self._push(client, coordinator.journal.read())
        assert [r["status"] for r in echoed["results"]] == ["duplicate"]  # type: ignore[index,union-attr]


# ---------------------------------------------------------------------------
# bernstein cluster claims log | head | verify
# ---------------------------------------------------------------------------


class TestClaimsCli:
    """Offline journal inspection. No server is started by any test here."""

    def _journal_with(self, tmp_path: Path, *, entries: int = 2) -> Path:
        sdd = tmp_path / ".sdd"
        path = default_claim_journal_path(sdd)
        coordinator = MeshCoordinator(
            journal=ClaimJournal(
                path,
                kms_adapter=_kms(tmp_path, seed=3, name="cli-key"),  # type: ignore[arg-type]
                node_id="node-cli",
            ),
        )
        for index in range(entries):
            coordinator.claim(
                tracker="jira",
                ticket_id=f"T-{index}",
                role="backend",
                claimer_id=f"w-{index}",
                now=1000.0 + index,
            )
        return path

    def test_log_lists_receipts_as_json(self, tmp_path: Path) -> None:
        self._journal_with(tmp_path, entries=2)
        result = CliRunner().invoke(
            cluster_group,
            ["claims", "log", "--workdir", str(tmp_path), "--json-output"],
        )
        assert result.exit_code == 0, result.output
        receipts = json.loads(result.output)
        assert len(receipts) == 2
        assert {r["kind"] for r in receipts} == {"claim"}

    def test_head_prints_head_hash_and_count(self, tmp_path: Path) -> None:
        path = self._journal_with(tmp_path, entries=3)
        expected = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])["entry_hash"]
        result = CliRunner().invoke(
            cluster_group,
            ["claims", "head", "--workdir", str(tmp_path), "--json-output"],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["head"] == expected
        assert payload["entries"] == 3

    def test_verify_clean_journal_exits_zero_and_prints_head(self, tmp_path: Path) -> None:
        self._journal_with(tmp_path, entries=2)
        result = CliRunner().invoke(
            cluster_group,
            ["claims", "verify", "--workdir", str(tmp_path), "--no-check-anchors", "--json-output"],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["ok"] is True
        assert payload["clean"] is True
        assert payload["entries"] == 2
        assert payload["head"].startswith("sha256:")

    def test_verify_reports_the_failing_entry_index_on_tamper(self, tmp_path: Path) -> None:
        path = self._journal_with(tmp_path, entries=3)
        lines = path.read_bytes().splitlines()
        lines[1] = lines[1].replace(b'"w-1"', b'"w-X"')
        path.write_bytes(b"\n".join(lines) + b"\n")

        result = CliRunner().invoke(
            cluster_group,
            ["claims", "verify", "--workdir", str(tmp_path), "--no-check-anchors", "--json-output"],
        )
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["ok"] is False
        assert payload["bad_index"] == 1

    def test_verify_exits_two_when_the_journal_carries_a_fork(self, tmp_path: Path) -> None:
        path = self._journal_with(tmp_path, entries=1)
        journal = ClaimJournal(
            path,
            kms_adapter=_kms(tmp_path, seed=3, name="cli-key"),  # type: ignore[arg-type]
            node_id="node-cli",
        )
        peer = _peer(tmp_path)
        peer.claim(tracker="jira", ticket_id="T-remote", role="qa", claimer_id="w-peer", now=1000.0)
        outcome = journal.ingest(peer.journal.read()[0], ts_ns=1_200_000_000)
        assert outcome.status == "forked"

        result = CliRunner().invoke(
            cluster_group,
            ["claims", "verify", "--workdir", str(tmp_path), "--no-check-anchors", "--json-output"],
        )
        assert result.exit_code == 2
        payload = json.loads(result.output)
        assert payload["ok"] is True
        assert payload["clean"] is False
        assert payload["forks"][0]["divergence_index"] == 0

    def test_missing_journal_is_an_actionable_error(self, tmp_path: Path) -> None:
        result = CliRunner().invoke(cluster_group, ["claims", "head", "--workdir", str(tmp_path)])
        assert result.exit_code != 0
        assert "no claim journal" in result.output
        assert "mesh" in result.output

    def test_explicit_journal_path_verifies_a_copied_file(self, tmp_path: Path) -> None:
        """A journal lifted off a dead machine verifies with no project around."""
        source = self._journal_with(tmp_path, entries=2)
        copied = tmp_path / "evidence" / "claim_journal.jsonl"
        copied.parent.mkdir(parents=True)
        copied.write_bytes(source.read_bytes())

        result = CliRunner().invoke(
            cluster_group,
            ["claims", "verify", "--journal", str(copied), "--no-check-anchors", "--json-output"],
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["entries"] == 2


@pytest.mark.parametrize("subcommand", ["log", "head", "verify"])
def test_claims_subcommands_are_registered(subcommand: str) -> None:
    """All three documented subcommands exist under ``cluster claims``."""
    result = CliRunner().invoke(cluster_group, ["claims", subcommand, "--help"])
    assert result.exit_code == 0, result.output
