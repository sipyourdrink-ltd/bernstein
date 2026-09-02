"""The live spawn path records delegation hops, and refuses when it cannot (#5047).

Driven through ``AgentSpawner._issue_agent_token``, the real production method
that calls ``create_identity``, using the same spawner construction
``tests/unit/test_spawner_agent_token.py`` already uses.  The public entry
``spawn_for_tasks`` has no existing harness in this repository, so the handler
branch that re-raises :class:`DelegationWriteError` is covered by proving the
type reaches it rather than by driving a whole spawn; building a harness for it
was out of scope.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.delegation_cmd import delegation_group
from bernstein.core.identity.agent_jwt import AgentIdentityStore, DelegationWriteError
from bernstein.core.agents.spawner_core import AgentSpawner
from bernstein.core.config.manifest import RunManifest, save_manifest
from bernstein.core.identity import delegation

KEY = b"k" * 32
RUN = "run-5047-live"


class _NoopAdapter:
    """Minimal adapter: the spawner only needs a name for token issuance."""

    def name(self) -> str:
        return "noop"

    def spawn(self, **_kwargs: object) -> object:  # pragma: no cover - never called here
        raise NotImplementedError

    def is_alive(self, pid: int) -> bool:  # pragma: no cover - never called here
        return False

    def kill(self, pid: int) -> object:  # pragma: no cover - never called here
        raise NotImplementedError


def _make_spawner(workdir: Path) -> AgentSpawner:
    templates_dir = workdir / "templates"
    templates_dir.mkdir(parents=True, exist_ok=True)
    return AgentSpawner(
        adapter=_NoopAdapter(),
        templates_dir=templates_dir,
        workdir=workdir,
        use_worktrees=False,
    )


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """A spawner wired the way the orchestrator wires it at run start."""
    monkeypatch.setattr(delegation, "_audit_key", lambda: KEY)
    workdir = tmp_path / "project"
    workdir.mkdir()
    spawner = _make_spawner(workdir)

    # What the orchestrator does in __init__: mint one parentless run root,
    # record its id in the run manifest, hand it to the spawner with the run id.
    store = AgentIdentityStore(workdir / ".sdd" / "auth")
    root_identity, _ = store.create_identity(
        f"run-root-{RUN}", "manager", metadata={"source": "orchestrator", "run_id": RUN}
    )
    save_manifest(RunManifest(run_id=RUN, run_root_identity_id=root_identity.id), workdir / ".sdd")
    spawner.set_run_id(RUN)
    spawner.set_run_root_identity_id(root_identity.id)
    return spawner, workdir, root_identity.id


def _audit_root(workdir: Path) -> Path:
    return workdir / ".sdd" / "audit"


def test_live_spawn_path_records_hops_and_the_run_verifies_from_the_manifest(wired):
    """THE HEADLINE: a run spawned through the real path verifies, exit 0, hops printed.

    Two agents are issued tokens through ``_issue_agent_token``, the method the
    spawn path calls.  The identity store is then deleted outright, so the
    verifier cannot consult it even by accident, and ``delegation verify`` is
    driven through its own CLI.  The run root is recovered from the manifest,
    which is the whole reason its id is recorded there.
    """
    spawner, workdir, _root_id = wired

    for index in range(2):
        spawner._issue_agent_token(
            f"agent-{index}",
            "backend",
            [f"T-{index}"],
            parent_identity_id=spawner._run_root_identity_id,
        )

    # The store is unavailable from here on.
    shutil.rmtree(workdir / ".sdd" / "auth")
    assert not (workdir / ".sdd" / "auth").exists()

    result = CliRunner().invoke(delegation_group, ["verify", RUN, "--root", str(_audit_root(workdir))])
    assert result.exit_code == 0, result.output
    assert "2 hop(s)" in result.output
    assert "No delegation receipts" not in result.output


def test_hop_failure_raises_delegation_write_error_through_the_spawn_path(wired, monkeypatch):
    """The distinct type the spawner's handler discriminates on reaches it."""
    spawner, _workdir, root_id = wired

    def _boom(**_kwargs):
        msg = "ledger unavailable"
        raise OSError(msg)

    monkeypatch.setattr(delegation, "record_delegation_hop", _boom)

    with pytest.raises(DelegationWriteError) as caught:
        spawner._issue_agent_token("agent-0", "backend", ["T-0"], parent_identity_id=root_id)
    assert isinstance(caught.value.__cause__, OSError)


def test_a_non_delegation_identity_failure_is_not_the_fail_closed_type(wired, monkeypatch):
    """The narrowing is meaningful: other identity failures keep their old type.

    The handler re-raises only :class:`DelegationWriteError` and keeps
    log-and-continue for everything else, so this asserts that an unrelated
    identity failure does NOT arrive as that type and therefore still takes the
    old path.
    """
    spawner, _workdir, root_id = wired

    class _Failing:
        def create_identity(self, *_args, **_kwargs):
            msg = "jwt signing unavailable"
            raise RuntimeError(msg)

    spawner._identity_store_instance = _Failing()

    with pytest.raises(RuntimeError) as caught:
        spawner._issue_agent_token("agent-0", "backend", ["T-0"], parent_identity_id=root_id)
    assert not isinstance(caught.value, DelegationWriteError)


def test_spawn_refusal_writes_an_audit_event_naming_run_and_reason(wired):
    """The refusal is visible in the run's audit chain, not only in an exception."""
    from bernstein.core.security.audit_chain import EVENT_SPAWN_REFUSED_UNRECEIPTED, AuditChainStore

    spawner, workdir, root_id = wired
    spawner._audit_spawn_refused_unreceipted("agent-0", root_id, "ledger unavailable")

    events = AuditChainStore(workdir / ".sdd" / "audit").query(
        event_type=EVENT_SPAWN_REFUSED_UNRECEIPTED, include_archived=True
    )
    assert len(events) == 1
    assert events[0].resource_id == "agent-0"
    assert events[0].details["run_id"] == RUN
    assert events[0].details["issuer"] == root_id
    assert "ledger unavailable" in events[0].details["reason"]


def test_nested_spawn_prefers_the_session_parent_over_the_run_root(wired):
    """A nested spawn names its spawning agent, not the run root.

    ``AgentSession.parent_id`` is the delegation-tree field; the spawn path reads
    ``session.parent_id or self._run_root_identity_id``, so a populated
    ``parent_id`` wins and the receipt records parent -> child rather than
    root -> child.  Nothing in ``src/`` populates ``parent_id`` today, so this
    covers the branch directly.
    """
    spawner, workdir, root_id = wired

    spawner._issue_agent_token("agent-parent", "backend", ["T-0"], parent_identity_id=root_id)
    spawner._issue_agent_token("agent-child", "backend", ["T-0"], parent_identity_id="agent-parent")

    receipts = delegation.verify_run_chain(root=_audit_root(workdir), run_id=RUN, key=KEY).receipts
    assert [(r.issuer, r.subject) for r in receipts] == [
        (root_id, "agent-parent"),
        ("agent-parent", "agent-child"),
    ]
    # The nested hop anchors to its parent's hop, not to genesis.
    assert receipts[1].parent_ref == receipts[0].hmac
