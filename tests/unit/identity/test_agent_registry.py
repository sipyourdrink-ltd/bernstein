"""Tests for the agent-principal registry projection (issue #4969).

The registry is a fold over the grant and delegation chains, not a second
source of truth. These tests pin that: the projection names exactly the
principals the chains name, rebuilding it after deletion reproduces the same
bytes, an expired grant no longer contributes to the ceiling in force, and an
entry with no supporting chain event is refused rather than invented.
"""

from __future__ import annotations

import json

import pytest

from bernstein.core.identity import agent_registry, delegation, grants
from bernstein.core.identity.delegation_scope import DelegationScope
from tests.support.delegation import close_manifest

KEY = b"k" * 32
NOW = 1_800_000_000


@pytest.fixture
def signer() -> grants.GrantSigner:
    return grants.GrantSigner.generate(issuer="manager:test")


@pytest.fixture
def chain_root(tmp_path, signer):
    """Write a fixture chain naming three principals across both ledgers."""
    root = tmp_path / "audit"
    grant_ledger = grants.GrantLedger(root=root, key=KEY, signer=signer)
    grant_ledger.issue_grant(
        run_id="run-1",
        task_id="t-1",
        secret_name="ANTHROPIC_API_KEY",
        audience="sub-agent-backend",
        expiry=NOW + 3600,
        capability_ceiling=("read",),
        grant_id="g-live",
        created=NOW - 100,
    )
    deleg = delegation.DelegationLedger(root=root, key=KEY)
    deleg.record_hop(
        run_id="run-1",
        issuer="principal-alex",
        subject="orchestrator",
        audience="orchestrator",
        act="run.authorize",
        created=NOW - 90,
        scope=DelegationScope(permissions=frozenset({"tasks:read", "files:write"})),
    )
    deleg.record_hop(
        run_id="run-1",
        issuer="orchestrator",
        subject="orchestrator",
        audience="sub-agent-backend",
        act="task.spawn",
        created=NOW - 80,
        scope=DelegationScope(permissions=frozenset({"tasks:read"})),
    )
    close_manifest(deleg, "run-1")
    return root


def _project(root, **kwargs):
    return agent_registry.project_agents(root=root, key=KEY, now=NOW, **kwargs)


class TestProjectionCoversTheChain:
    def test_projection_lists_every_chain_principal_and_no_others(self, chain_root) -> None:
        projection = _project(chain_root)
        assert [a.agent_id for a in projection.agents] == [
            "manager:test",
            "orchestrator",
            "principal-alex",
            "sub-agent-backend",
        ]
        assert projection.errors == ()

    def test_every_listed_agent_names_the_chain_events_that_established_it(self, chain_root) -> None:
        projection = _project(chain_root)
        backend = projection.agent("sub-agent-backend")
        assert backend is not None
        sources = {(e.source, e.run_id, e.index, e.role) for e in backend.chain_events}
        assert ("grant", "run-1", 0, "audience") in sources
        assert ("delegation", "run-1", 1, "audience") in sources
        assert backend.grants == ("g-live",)
        assert all(e.anchor for e in backend.chain_events)

    def test_unverifiable_chain_establishes_no_principals(self, chain_root) -> None:
        path = chain_root / "delegation" / "run-1.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        tampered = json.loads(lines[0])
        tampered["audience"] = "sub-agent-smuggled"
        lines[0] = json.dumps(tampered, sort_keys=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        projection = _project(chain_root)
        assert "sub-agent-smuggled" not in [a.agent_id for a in projection.agents]
        assert "principal-alex" not in [a.agent_id for a in projection.agents]
        assert any("delegation/run-1" in err for err in projection.errors)


class TestProjectionIsAFoldNotAStore:
    def test_rebuilding_the_deleted_projection_is_byte_identical(self, chain_root, tmp_path) -> None:
        out = tmp_path / "agents.json"
        out.write_bytes(agent_registry.render_registry(_project(chain_root)).encode("utf-8"))
        first = out.read_bytes()

        out.unlink()
        assert not out.exists()

        out.write_bytes(agent_registry.render_registry(_project(chain_root)).encode("utf-8"))
        assert out.read_bytes() == first

    def test_expired_grant_drops_from_the_ceiling_in_force_now(self, tmp_path, signer) -> None:
        root = tmp_path / "audit"
        ledger = grants.GrantLedger(root=root, key=KEY, signer=signer)
        ledger.issue_grant(
            run_id="run-1",
            task_id="t-1",
            secret_name="K",
            audience="sub-agent-backend",
            expiry=NOW - 1,
            capability_ceiling=("write",),
            grant_id="g-expired",
            created=NOW - 5_000,
        )
        ledger.issue_grant(
            run_id="run-1",
            task_id="t-2",
            secret_name="K",
            audience="sub-agent-backend",
            expiry=NOW + 5_000,
            capability_ceiling=("read",),
            grant_id="g-live",
            created=NOW - 100,
        )

        backend = _project(root).agent("sub-agent-backend")
        assert backend is not None
        # Both grants are listed -- the chain recorded both -- but only the
        # unexpired one contributes to the ceiling in force now.
        assert backend.grants == ("g-expired", "g-live")
        assert "files:write" not in backend.capability_ceiling
        assert "files:read" in backend.capability_ceiling

    def test_revoked_grant_drops_from_the_ceiling_in_force_now(self, tmp_path, signer) -> None:
        root = tmp_path / "audit"
        ledger = grants.GrantLedger(root=root, key=KEY, signer=signer)
        ledger.issue_grant(
            run_id="run-1",
            task_id="t-1",
            secret_name="K",
            audience="sub-agent-backend",
            expiry=NOW + 5_000,
            capability_ceiling=("write",),
            grant_id="g-revoked",
            created=NOW - 100,
        )
        ledger.revoke_grant(run_id="run-1", grant_id="g-revoked", reason="rotated", created=NOW - 50)

        backend = _project(root).agent("sub-agent-backend")
        assert backend is not None
        assert backend.capability_ceiling == ()


class TestRefusalRatherThanInvention:
    def test_agent_absent_from_the_chain_is_refused_not_listed(self, chain_root, tmp_path) -> None:
        out = tmp_path / "agents.json"
        payload = json.loads(agent_registry.render_registry(_project(chain_root)))
        payload["agents"].append(
            {
                "agent_id": "sub-agent-ghost",
                "capability_ceiling": ["files:write"],
                "chain_events": [],
                "delegations": [],
                "grants": [],
                "spiffe_id": None,
            }
        )
        out.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")), encoding="utf-8")

        verification = agent_registry.verify_registry(out, root=chain_root, key=KEY, now=NOW)
        assert not verification.ok
        assert verification.invented == ("sub-agent-ghost",)
        assert "sub-agent-ghost" in verification.reason

    def test_principal_without_a_supporting_chain_event_cannot_be_constructed(self) -> None:
        with pytest.raises(agent_registry.RegistryError, match="sub-agent-ghost"):
            agent_registry.AgentPrincipal(agent_id="sub-agent-ghost", chain_events=())

    def test_verify_registry_accepts_a_projection_it_recomputes(self, chain_root, tmp_path) -> None:
        out = tmp_path / "agents.json"
        out.write_text(agent_registry.render_registry(_project(chain_root)), encoding="utf-8")
        verification = agent_registry.verify_registry(out, root=chain_root, key=KEY, now=NOW)
        assert verification.ok
        assert verification.invented == ()
        assert verification.missing == ()


class TestSpiffeProjection:
    def test_spiffe_id_is_derived_only_for_a_valid_path_segment(self, chain_root) -> None:
        projection = _project(
            chain_root,
            trust_domain="example.org",
            install_public_key_pem=b"-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----\n",
        )
        backend = projection.agent("sub-agent-backend")
        manager = projection.agent("manager:test")
        assert backend is not None and manager is not None
        assert backend.spiffe_id is not None
        assert backend.spiffe_id.startswith("spiffe://example.org/bernstein/")
        assert backend.spiffe_id.endswith("/sub-agent-backend")
        # "manager:test" is not a SPIFFE path segment; no name is coined for it.
        assert manager.spiffe_id is None


class TestIdentityAgentsCommand:
    def test_identity_agents_renders_the_projection_as_canonical_json(self, chain_root, monkeypatch) -> None:
        from click.testing import CliRunner

        from bernstein.cli.commands import identity_cmd

        monkeypatch.setattr(identity_cmd, "_audit_key_for_read", lambda: KEY)
        result = CliRunner().invoke(
            identity_cmd.identity_group,
            ["agents", "--root", str(chain_root), "--json", "--as-of", str(NOW)],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert [a["agent_id"] for a in payload["agents"]] == [
            "manager:test",
            "orchestrator",
            "principal-alex",
            "sub-agent-backend",
        ]

    def test_identity_agents_verify_exits_two_on_an_invented_entry(self, chain_root, tmp_path, monkeypatch) -> None:
        from click.testing import CliRunner

        from bernstein.cli.commands import identity_cmd

        monkeypatch.setattr(identity_cmd, "_audit_key_for_read", lambda: KEY)
        out = tmp_path / "agents.json"
        payload = json.loads(agent_registry.render_registry(_project(chain_root)))
        payload["agents"].append(
            {
                "agent_id": "sub-agent-ghost",
                "capability_ceiling": [],
                "chain_events": [],
                "delegations": [],
                "grants": [],
                "spiffe_id": None,
            }
        )
        out.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")), encoding="utf-8")

        result = CliRunner().invoke(
            identity_cmd.identity_group,
            ["agents", "--root", str(chain_root), "--as-of", str(NOW), "--verify", str(out)],
        )
        assert result.exit_code == 2, result.output
        assert "sub-agent-ghost" in result.output


@pytest.mark.parametrize("legacy", [False, True])
def test_incomplete_runs_retain_delegated_ceilings(chain_root, legacy):
    from bernstein.core.config.manifest import RunManifest, save_manifest

    path = chain_root.parent / "runtime" / "manifests" / "run-1.json"
    if legacy:
        path.unlink()  # A pre-upgrade run may have no manifest at all.
    else:
        manifest = RunManifest(run_id="run-1")
        object.__setattr__(manifest, "manifest_hash", manifest.compute_hash())
        save_manifest(manifest, chain_root.parent)
    projection = _project(chain_root)
    assert projection.errors == ()
    assert projection.incomplete_runs == ("run-1",)
    assert projection.agent("orchestrator").capability_ceiling == ("files:write", "tasks:read")
    assert "tasks:read" in projection.agent("sub-agent-backend").capability_ceiling


def test_pre_upgrade_snapshot_still_verifies(tmp_path):
    """Build the old version-1 wire format directly, without the new projector."""
    root = tmp_path / "audit"
    ledger = delegation.DelegationLedger(root, KEY)
    receipt = ledger.record_hop(
        run_id="legacy",
        issuer="owner",
        subject="worker",
        audience="worker",
        act="spawn",
        created=NOW,
        scope=DelegationScope(permissions=frozenset({"tasks:read"})),
    )

    def event(role):
        return {
            "source": "delegation",
            "run_id": "legacy",
            "index": 0,
            "kind": "spawn",
            "role": role,
            "anchor": receipt.hmac,
        }

    snapshot = {
        "version": 1,
        "as_of": NOW,
        "errors": [],
        "agents": [
            {
                "agent_id": "owner",
                "capability_ceiling": [],
                "chain_events": [event("issuer")],
                "delegations": ["legacy#0"],
                "grants": [],
                "spiffe_id": None,
            },
            {
                "agent_id": "worker",
                "capability_ceiling": ["tasks:read"],
                "chain_events": [event("audience"), event("subject")],
                "delegations": [],
                "grants": [],
                "spiffe_id": None,
            },
        ],
    }
    path = tmp_path / "pre-upgrade.json"
    path.write_text(json.dumps(snapshot))
    assert agent_registry.verify_registry(path, root=root, key=KEY, now=NOW).ok
    # Compatibility must not forgive an altered authority ceiling.
    snapshot["agents"][1]["capability_ceiling"].append("files:write")
    path.write_text(json.dumps(snapshot))
    assert not agent_registry.verify_registry(path, root=root, key=KEY, now=NOW).ok


def test_truncated_closed_run_contributes_no_delegated_ceilings(chain_root):
    path = chain_root / "delegation" / "run-1.jsonl"
    path.write_text(path.read_text().splitlines()[0] + "\n")
    projection = _project(chain_root)
    assert projection.agent("orchestrator") is None
    assert any("mismatch" in error for error in projection.errors)
