"""A fan-out's branches must pair with the spines that recorded them.

Covers ``src/bernstein/core/lineage/run_graph.py``. Before it existed, a
``ClassifiedWorktree`` carried no ``head_sha`` and no ``run_id``, and a
``LineageSpine`` was indexed by ``run_id`` with no way back to the worktree
whose writes it held - so for N branches of one fan-out there was no single
call returning, per branch, its git state and the spine that attested it.

Three properties are pinned here, because each fails differently:

* the pairing itself - every branch gets the *right* head sha and spine head,
  not merely some pair;
* determinism of the root hash - two runs over byte-identical fixtures must
  agree, otherwise the root cannot anchor anything later;
* that an unresolvable branch is *reported*, not dropped. Silently omitting it
  would make a fan-out that lost a spine hash identically to one that never
  had that branch, which is exactly the case a lineage graph exists to catch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.lineage.run_graph import (
    RunGraphNodeStatus,
    build_run_graph,
    compute_root_hash,
)
from bernstein.core.lineage.spine import LineageSpine

HMAC_KEY = b"\x11" * 32
#: Fixed so the golden root hash below stays stable.
TIMESTAMP = 1_700_000_000

SESSIONS = ("sess-alpha", "sess-beta", "sess-gamma")
#: Distinguishable fabricated HEAD shas, one per session.
HEAD_SHAS = {
    "sess-alpha": "a" * 40,
    "sess-beta": "b" * 40,
    "sess-gamma": "c" * 40,
}
RUN_IDS = {session: f"run-{session.split('-')[1]}" for session in SESSIONS}


def _resolver(path: Path) -> str | None:
    """Stand in for git, keyed on the worktree directory name."""
    return HEAD_SHAS.get(path.name)


@pytest.fixture
def fanout(tmp_path: Path) -> tuple[Path, Path]:
    """Three worktree-shaped sessions, each with a spine holding one write.

    Returns ``(repo_root, lineage_root)``.
    """
    repo_root = tmp_path / "repo"
    worktrees = repo_root / ".sdd" / "runtime" / "worktrees"
    worktrees.mkdir(parents=True)
    lineage_root = tmp_path / "lineage"

    for session in SESSIONS:
        (worktrees / session).mkdir()
        spine = LineageSpine(lineage_root, run_id=RUN_IDS[session], hmac_key=HMAC_KEY)
        spine.record(
            artifact_path=f"out/{session}.txt",
            content=f"written by {session}".encode(),
            actor="tester",
            step_id="step-1",
            model="test-model",
            timestamp=TIMESTAMP,
        )
    return repo_root, lineage_root


def _build(repo_root: Path, lineage_root: Path, **overrides: object) -> object:
    kwargs: dict[str, object] = {
        "run_ids": dict(RUN_IDS),
        "lineage_root": lineage_root,
        "hmac_key": HMAC_KEY,
        "head_sha_resolver": _resolver,
    }
    kwargs.update(overrides)
    return build_run_graph(repo_root, **kwargs)  # type: ignore[arg-type]


def test_each_branch_pairs_with_its_own_spine(fanout: tuple[Path, Path]) -> None:
    repo_root, lineage_root = fanout

    graph = _build(repo_root, lineage_root)

    assert len(graph.nodes) == 3
    assert [n.session_id for n in graph.nodes] == sorted(SESSIONS)
    for node in graph.nodes:
        assert node.status is RunGraphNodeStatus.RESOLVED
        assert node.head_sha == HEAD_SHAS[node.session_id]
        assert node.run_id == RUN_IDS[node.session_id]
        expected = LineageSpine(lineage_root, run_id=RUN_IDS[node.session_id], hmac_key=HMAC_KEY).head_hash()
        assert node.spine_head_hash == expected
        assert node.spine_head_hash


def test_spine_heads_are_distinct_per_branch(fanout: tuple[Path, Path]) -> None:
    """Guards the pairing against an off-by-one that reads one spine thrice."""
    repo_root, lineage_root = fanout

    graph = _build(repo_root, lineage_root)

    heads = [n.spine_head_hash for n in graph.nodes]
    assert len(set(heads)) == 3, heads


def test_root_hash_is_deterministic(fanout: tuple[Path, Path]) -> None:
    repo_root, lineage_root = fanout

    first = _build(repo_root, lineage_root)
    second = _build(repo_root, lineage_root)

    assert first.root_hash == second.root_hash
    assert first.root_hash.startswith("sha256:")


def test_root_hash_changes_when_a_head_sha_changes(fanout: tuple[Path, Path]) -> None:
    """A root that ignored head shas could not anchor git state."""
    repo_root, lineage_root = fanout
    baseline = _build(repo_root, lineage_root)

    moved = _build(
        repo_root,
        lineage_root,
        head_sha_resolver=lambda p: "d" * 40 if p.name == "sess-beta" else _resolver(p),
    )

    assert moved.root_hash != baseline.root_hash


def test_unresolved_session_is_reported_not_dropped(fanout: tuple[Path, Path]) -> None:
    """The branch stays in the graph, marked, and still moves the root."""
    repo_root, lineage_root = fanout
    partial = {k: v for k, v in RUN_IDS.items() if k != "sess-beta"}

    graph = _build(repo_root, lineage_root, run_ids=partial)

    assert len(graph.nodes) == 3
    orphan = next(n for n in graph.nodes if n.session_id == "sess-beta")
    assert orphan.status is RunGraphNodeStatus.UNRESOLVED
    assert orphan.run_id is None
    assert orphan.spine_head_hash is None
    # Still carries its git state - losing the spine does not lose the branch.
    assert orphan.head_sha == HEAD_SHAS["sess-beta"]
    assert graph.root_hash != _build(repo_root, lineage_root).root_hash


def test_absent_is_distinguishable_from_an_empty_spine_head() -> None:
    """An empty spine head is a real value; ``None`` must not collide with it.

    A run with no entries returns ``""`` from ``head_hash()``. If the root
    pre-image mapped ``None`` to ``""`` as well, a branch whose spine was lost
    would hash identically to one whose spine is merely empty.
    """
    from bernstein.core.lineage.run_graph import RunGraphNode

    empty_head = RunGraphNode("s", "a" * 40, "run-1", "", RunGraphNodeStatus.RESOLVED)
    no_head = RunGraphNode("s", "a" * 40, None, None, RunGraphNodeStatus.UNRESOLVED)

    assert compute_root_hash((empty_head,)) != compute_root_hash((no_head,))


def test_node_order_does_not_depend_on_input_order(fanout: tuple[Path, Path]) -> None:
    """The root hashes the sorted triples, so shuffling inputs cannot move it."""
    repo_root, lineage_root = fanout
    graph = _build(repo_root, lineage_root)

    shuffled = tuple(reversed(graph.nodes))

    assert compute_root_hash(shuffled) == graph.root_hash


def test_root_pre_image_includes_session_id(fanout: tuple[Path, Path]) -> None:
    """Two branches sharing a pair are still two distinct branches."""
    from bernstein.core.lineage.run_graph import RunGraphNode

    one = RunGraphNode("sess-a", "a" * 40, "run-1", "sha256:x", RunGraphNodeStatus.RESOLVED)
    two = RunGraphNode("sess-b", "a" * 40, "run-1", "sha256:x", RunGraphNodeStatus.RESOLVED)

    assert compute_root_hash((one,)) != compute_root_hash((two,))


def test_empty_repo_yields_an_empty_graph(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / ".sdd" / "runtime" / "worktrees").mkdir(parents=True)

    graph = _build(repo_root, tmp_path / "lineage")

    assert graph.nodes == ()
    assert graph.root_hash == compute_root_hash(())
