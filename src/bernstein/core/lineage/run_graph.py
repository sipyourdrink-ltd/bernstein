"""Pair each worktree branch of a fan-out with the spine that recorded it.

A fan-out leaves N worktrees behind, and a :class:`~bernstein.core.lineage.spine.LineageSpine`
per run records every artifact write. The two halves were never joined: a
:class:`~bernstein.core.worktrees.classifier.ClassifiedWorktree` carries no
``head_sha`` and no ``run_id``, and a spine is indexed by ``run_id`` with no
back-reference to the worktree whose writes it holds. So no single call could
answer, per branch, *what git state it held* and *which spine attested it*.

:func:`build_run_graph` composes the existing primitives into that answer. It
adds no storage: the branch list comes from ``classify_worktrees``, the head
sha from git, and the spine head from ``LineageSpine.head_hash()``. The graph
root is a content hash over the sorted ``(head_sha, spine_head_hash)`` pairs,
so two runs over byte-identical inputs produce the same root.

Resolving ``session_id`` to ``run_id``
-------------------------------------

Nothing in the repository records that mapping, so it is supplied by the
caller as ``run_ids``. The alternative - teaching the spawner to write a
``run_id`` into the PID record that ``_read_pid_record`` already parses - was
rejected for two reasons. It changes the spawn path, which is outside this
slice; and it is only true going forward, so every worktree created before
the change would resolve as unresolved. That would bake a migration into the
data rather than into one caller. Passing the mapping in also keeps this
function pure, which is what makes the root hash reproducible under test.

A session with no entry in ``run_ids`` is **not** dropped. It becomes a node
with :data:`RunGraphNodeStatus.UNRESOLVED` and contributes to the root hash
under its own sentinel, so a fan-out that silently lost a spine hashes
differently from one that never had that branch.
"""

from __future__ import annotations

import enum
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bernstein.core.lineage.spine import LineageSpine, content_hash_of
from bernstein.core.worktrees.classifier import _git_head_sha, classify_worktrees

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

#: Stands in for a missing ``head_sha`` or ``spine_head_hash`` in the root
#: pre-image. A literal empty string would let "absent" and "recorded as
#: empty" collide, and an empty spine head *is* the empty string.
ABSENT = "\x00absent"


class RunGraphNodeStatus(enum.Enum):
    """Whether a branch could be paired with a spine."""

    #: ``run_id`` known and its spine read.
    RESOLVED = "resolved"
    #: No ``run_id`` for this session; the node is kept and marked.
    UNRESOLVED = "unresolved"


@dataclass(frozen=True, slots=True)
class RunGraphNode:
    """One branch of a fan-out, paired with the spine that recorded it.

    Attributes:
        session_id: Worktree session id, from the classifier.
        head_sha: Full git HEAD sha of the worktree, or ``None`` when git
            could not resolve one (detached, unborn, corrupt, or missing).
        run_id: Run whose spine recorded this branch's writes, or ``None``
            when the caller supplied no mapping for this session.
        spine_head_hash: That spine's current chain head, or ``None`` when
            ``run_id`` is ``None``. An empty string is a *valid* value - it
            is what an empty run's spine returns.
        status: :class:`RunGraphNodeStatus`.
    """

    session_id: str
    head_sha: str | None
    run_id: str | None
    spine_head_hash: str | None
    status: RunGraphNodeStatus


@dataclass(frozen=True, slots=True)
class RunGraph:
    """Every branch of one fan-out, plus a hash over their pairs.

    Attributes:
        nodes: One :class:`RunGraphNode` per worktree, ordered by
            ``session_id`` so the sequence does not depend on directory
            iteration order.
        root_hash: ``sha256:``-prefixed hash over the sorted
            ``(session_id, head_sha, spine_head_hash)`` triples.
    """

    nodes: tuple[RunGraphNode, ...]
    root_hash: str


def compute_root_hash(nodes: tuple[RunGraphNode, ...]) -> str:
    """Hash the ``(session_id, head_sha, spine_head_hash)`` triples.

    ``session_id`` is part of the pre-image, not just the sort key: two
    branches that happen to share a head sha and a spine head are still
    distinct branches, and a root that ignored the id could not tell a
    renamed session from an unchanged one.
    """
    payload = [
        [
            node.session_id,
            ABSENT if node.head_sha is None else node.head_sha,
            ABSENT if node.spine_head_hash is None else node.spine_head_hash,
        ]
        for node in sorted(nodes, key=lambda n: n.session_id)
    ]
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return content_hash_of(canonical.encode("utf-8"))


def build_run_graph(
    repo_root: Path,
    *,
    run_ids: Mapping[str, str],
    lineage_root: Path,
    hmac_key: bytes,
    head_sha_resolver: Callable[[Path], str | None] = _git_head_sha,
) -> RunGraph:
    """Assemble a :class:`RunGraph` for every worktree under ``repo_root``.

    Args:
        repo_root: Repository root whose worktrees are classified.
        run_ids: ``session_id -> run_id``. Sessions absent from this mapping
            become ``UNRESOLVED`` nodes rather than being dropped.
        lineage_root: Root under which each run's spine directory lives.
        hmac_key: Key the spines were written with, needed to open them.
        head_sha_resolver: Injection point for git HEAD resolution; defaults
            to the classifier's own resolver.

    Returns:
        A :class:`RunGraph` whose ``nodes`` are ordered by ``session_id``.
    """
    nodes: list[RunGraphNode] = []
    for worktree in classify_worktrees(repo_root):
        run_id = run_ids.get(worktree.session_id)
        if run_id is None:
            nodes.append(
                RunGraphNode(
                    session_id=worktree.session_id,
                    head_sha=head_sha_resolver(worktree.path),
                    run_id=None,
                    spine_head_hash=None,
                    status=RunGraphNodeStatus.UNRESOLVED,
                )
            )
            continue
        spine = LineageSpine(lineage_root, run_id=run_id, hmac_key=hmac_key)
        nodes.append(
            RunGraphNode(
                session_id=worktree.session_id,
                head_sha=head_sha_resolver(worktree.path),
                run_id=run_id,
                spine_head_hash=spine.head_hash(),
                status=RunGraphNodeStatus.RESOLVED,
            )
        )

    ordered = tuple(sorted(nodes, key=lambda n: n.session_id))
    return RunGraph(nodes=ordered, root_hash=compute_root_hash(ordered))


__all__ = [
    "ABSENT",
    "RunGraph",
    "RunGraphNode",
    "RunGraphNodeStatus",
    "build_run_graph",
    "compute_root_hash",
]
