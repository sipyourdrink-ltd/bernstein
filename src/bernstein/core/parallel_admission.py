"""Parallel-execution admission from a code graph (#3237, scope step 3).

Whether two tasks may run at the same time is decided here, from their symbol
neighbourhoods rather than from their descriptions. A pair is admitted only
when both attributions are provable *and* their neighbourhoods do not
intersect.

Intersection is a scheduling constraint, not an error: the pair serialises. So
does an unprovable attribution. The two outcomes are recorded distinctly,
because "we proved these overlap" and "we could not prove they do not" are
different facts and an operator reading a run afterwards needs to tell them
apart.

Not to be confused with :mod:`bernstein.core.admission`, which is the
lease-backed resource-pool subsystem from #2544 -- a different question
(is there capacity?) about a different thing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from bernstein.core.knowledge.code_graph import TaskNodeSet

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

__all__ = [
    "ADMISSION_RECEIPT_VERSION",
    "ADMIT_PARALLEL",
    "RECEIPT_CONSISTENT_ONLY",
    "RECEIPT_DIVERGED",
    "RECEIPT_GRAPH_MISMATCH",
    "RECEIPT_VERIFIED",
    "SERIALISE_OVERLAP",
    "SERIALISE_UNPROVEN",
    "PairVerdict",
    "ReceiptVerification",
    "admit_pair",
    "build_admission_receipt",
    "serial_groups",
    "verify_admission_receipt",
]

#: Neighbourhoods are provably disjoint; the pair may run at once.
ADMIT_PARALLEL = "ADMIT_PARALLEL"

#: Neighbourhoods provably intersect. Running them in parallel is the case that
#: produces a merge conflict after both agents have already finished.
SERIALISE_OVERLAP = "SERIALISE_OVERLAP"

#: At least one attribution is ``UNPROVEN``, so disjointness is unknown.
#: Serialised out of caution, and recorded as caution rather than as a finding.
SERIALISE_UNPROVEN = "SERIALISE_UNPROVEN"


@dataclass(frozen=True)
class PairVerdict:
    """Why one pair of tasks may or may not run in parallel.

    Attributes:
        left: First task id, always the lexicographically smaller of the two.
        right: Second task id.
        verdict: :data:`ADMIT_PARALLEL`, :data:`SERIALISE_OVERLAP` or
            :data:`SERIALISE_UNPROVEN`.
        shared_symbols: The intersection, sorted. Empty unless the verdict is
            :data:`SERIALISE_OVERLAP`.
        reasons: Merged ``UNPROVEN`` reasons from either side, sorted. Empty
            unless the verdict is :data:`SERIALISE_UNPROVEN`.
    """

    left: str
    right: str
    verdict: str
    shared_symbols: tuple[str, ...]
    reasons: tuple[str, ...]

    @property
    def parallel(self) -> bool:
        """Whether the pair was admitted to run at the same time."""
        return self.verdict == ADMIT_PARALLEL

    def to_dict(self) -> dict[str, object]:
        """Canonical mapping for the receipt projection."""
        return {
            "left": self.left,
            "right": self.right,
            "verdict": self.verdict,
            "shared_symbols": list(self.shared_symbols),
            "reasons": list(self.reasons),
        }


def admit_pair(left: TaskNodeSet, right: TaskNodeSet) -> PairVerdict:
    """Decide whether two attributed tasks may run in parallel.

    Provability is checked before intersection, deliberately. An unprovable
    attribution has an unknown boundary, so an empty intersection against it
    means nothing -- reporting ``ADMIT_PARALLEL`` there would be the exact
    failure this design exists to prevent: a confident answer resting on a
    coverage hole.

    The pair is ordered by task id so the verdict for (a, b) and (b, a) is the
    same value, which is what lets a receipt be compared byte for byte.

    Args:
        left: One task's attribution.
        right: The other's.

    Returns:
        The verdict, with the evidence behind it.
    """
    first, second = sorted((left, right), key=lambda t: t.task_id)

    if not (first.proven and second.proven):
        reasons = set(first.reasons) | set(second.reasons)
        return PairVerdict(
            left=first.task_id,
            right=second.task_id,
            verdict=SERIALISE_UNPROVEN,
            shared_symbols=(),
            reasons=tuple(sorted(reasons)),
        )

    shared = set(first.neighborhood) & set(second.neighborhood)
    if shared:
        return PairVerdict(
            left=first.task_id,
            right=second.task_id,
            verdict=SERIALISE_OVERLAP,
            shared_symbols=tuple(sorted(shared)),
            reasons=(),
        )

    return PairVerdict(
        left=first.task_id,
        right=second.task_id,
        verdict=ADMIT_PARALLEL,
        shared_symbols=(),
        reasons=(),
    )


def _pairwise(items: Sequence[TaskNodeSet]) -> Iterable[tuple[TaskNodeSet, TaskNodeSet]]:
    for i, left in enumerate(items):
        for right in items[i + 1 :]:
            yield left, right


def _duplicate_task_ids(tasks: Sequence[TaskNodeSet]) -> tuple[str, ...]:
    """Return the task ids appearing more than once in *tasks*, sorted."""
    seen: set[str] = set()
    duplicates: set[str] = set()
    for task in tasks:
        if task.task_id in seen:
            duplicates.add(task.task_id)
        seen.add(task.task_id)
    return tuple(sorted(duplicates))


def _reject_duplicate_task_ids(tasks: Sequence[TaskNodeSet]) -> None:
    """Refuse an attribution set that names the same task twice.

    Two attributions sharing a task id are two different node sets under one
    name. The partition below is keyed by id, so it would silently keep one of
    them and emit a group listing the id twice -- a scheduling instruction that
    cannot be followed, and one a verifier reproduces exactly because it makes
    the same collapse. The set has to be rejected where it is built, not
    reported after it has been turned into groups.

    Args:
        tasks: The attribution set.

    Raises:
        ValueError: If any task id appears more than once.
    """
    duplicates = _duplicate_task_ids(tasks)
    if duplicates:
        raise ValueError(f"attributions name the same task more than once: {list(duplicates)}")


def serial_groups(attributions: Iterable[TaskNodeSet]) -> tuple[tuple[str, ...], ...]:
    """Partition tasks into groups whose members must not run concurrently.

    Every pair that is not admitted joins its members into one group; the
    groups that come out are the connected components of that relation. Tasks
    in different groups may run at the same time; tasks within a group are
    ordered by the caller.

    A single unprovable task lands in its own group with nobody else only when
    no other task exists, since ``SERIALISE_UNPROVEN`` against every peer will
    otherwise pull it together with all of them. That is the intended
    conservative behaviour: unknown coverage costs parallelism, never
    correctness.

    Args:
        attributions: One entry per task.

    Returns:
        Groups, each sorted, ordered by first member. Deterministic for any
        input order.

    Raises:
        ValueError: If two attributions share a task id.
    """
    tasks = sorted(attributions, key=lambda t: t.task_id)
    _reject_duplicate_task_ids(tasks)
    parent = {t.task_id: t.task_id for t in tasks}

    def find(node: str) -> str:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    for left, right in _pairwise(tasks):
        if not admit_pair(left, right).parallel:
            union(left.task_id, right.task_id)

    grouped: dict[str, list[str]] = {}
    for task in tasks:
        grouped.setdefault(find(task.task_id), []).append(task.task_id)

    return tuple(tuple(sorted(members)) for _, members in sorted(grouped.items()))


# ---------------------------------------------------------------------------
# Receipt projection (#3237, scope step 4)
# ---------------------------------------------------------------------------

#: Bumped when the receipt's shape changes, so a verifier never compares two
#: documents that only look alike. Travels inside the bytes, not beside them.
ADMISSION_RECEIPT_VERSION = 1

#: Every recorded verdict and every node set was reproduced from the stored
#: graph document. The only status that establishes the decision was correct.
RECEIPT_VERIFIED = "verified"

#: The recorded verdicts follow from the recorded node sets, but no graph
#: document was supplied, so the node sets themselves were not re-derived.
#: Reported distinctly rather than as success: it proves the receipt is
#: internally consistent and belongs to a named graph, and nothing about
#: whether those node sets are the ones that graph yields.
RECEIPT_CONSISTENT_ONLY = "consistent_only"

#: At least one recorded verdict differs from what the graph document yields.
#: Either the receipt was edited or it was not taken over this graph.
RECEIPT_DIVERGED = "diverged"

#: The receipt does not describe the graph it was handed. Reported separately
#: from a divergence because the operator's next move is different: find the
#: right graph rather than investigate a tampered decision.
RECEIPT_GRAPH_MISMATCH = "graph_mismatch"


@dataclass(frozen=True)
class ReceiptVerification:
    """Outcome of re-deriving an admission receipt.

    Attributes:
        status: :data:`RECEIPT_VERIFIED`, :data:`RECEIPT_CONSISTENT_ONLY`,
            :data:`RECEIPT_DIVERGED` or :data:`RECEIPT_GRAPH_MISMATCH`.
        divergences: Human-readable descriptions of each mismatch, sorted.
            Empty when verified.
    """

    status: str
    divergences: tuple[str, ...]

    @property
    def ok(self) -> bool:
        """Whether the receipt fully reproduced from the graph document.

        Deliberately False for :data:`RECEIPT_CONSISTENT_ONLY`. A caller that
        wants the weaker guarantee has to ask for it by name, so nobody gets
        it by writing ``if result.ok``.
        """
        return self.status == RECEIPT_VERIFIED


def build_admission_receipt(
    graph_digest_value: str,
    attributions: Iterable[TaskNodeSet],
) -> dict[str, object]:
    """Project the graph digest, the node sets and the verdicts into a receipt.

    The verdicts are included so a reader does not have to recompute them to
    see what happened, and :func:`verify_admission_receipt` recomputes them
    anyway so nobody has to trust that they are right. Recording a derived
    value and then re-deriving it is the point: the receipt is checkable, not
    authoritative.

    Args:
        graph_digest_value: Digest of the graph the decision was taken over.
        attributions: One entry per task in the run.

    Returns:
        A canonically-ordered mapping ready to be serialised into the lineage
        record.

    Raises:
        ValueError: If two attributions share a task id.
    """
    ordered = sorted(attributions, key=lambda t: t.task_id)
    _reject_duplicate_task_ids(ordered)
    verdicts = [admit_pair(left, right).to_dict() for left, right in _pairwise(ordered)]
    return {
        "version": ADMISSION_RECEIPT_VERSION,
        "graph_digest": graph_digest_value,
        "tasks": [task.to_dict() for task in ordered],
        "pairs": verdicts,
        "serial_groups": [list(group) for group in serial_groups(ordered)],
    }


def verify_admission_receipt(
    receipt: dict[str, object],
    *,
    graph_digest_value: str,
    graph_document_bytes: bytes | None = None,
) -> ReceiptVerification:
    """Re-derive a receipt and compare it to what was recorded.

    Nothing recorded is trusted. With *graph_document_bytes* the check is
    complete: the graph is rebuilt from the document, each task is
    re-attributed from its declared paths, and both the node sets and the
    verdicts must match. That is what makes an admission decision checkable by
    someone who has the receipt and the document and nothing else -- no
    workspace, no network, no live ``.sdd/``.

    Without the document only the recorded node sets can be re-folded into
    verdicts, which catches an edited verdict but not an edited node set. That
    outcome is :data:`RECEIPT_CONSISTENT_ONLY` and :attr:`ReceiptVerification.ok`
    is False for it, so the weaker guarantee cannot be mistaken for the
    stronger one by a caller writing ``if result.ok``.

    Either way the comparison covers the whole canonical projection, not only
    the verdicts: a receipt that reproduces its ``pairs`` while claiming a
    different ``version`` or carrying an edited ``tasks`` entry is not the
    document the run produced.

    Nothing here raises. A receipt no honest run could have produced is a
    divergence and a document the loader refuses is a mismatch, because a
    caller asking whether a decision holds is owed an answer rather than a
    traceback.

    Args:
        receipt: The recorded receipt.
        graph_digest_value: Digest of the graph to check it against.
        graph_document_bytes: The canonical graph document, when available.

    Returns:
        The verification outcome.
    """
    if receipt.get("graph_digest") != graph_digest_value:
        return ReceiptVerification(
            status=RECEIPT_GRAPH_MISMATCH,
            divergences=(
                f"receipt names graph {receipt.get('graph_digest')!r}, checked against {graph_digest_value!r}",
            ),
        )

    raw_tasks = receipt.get("tasks")
    entries = [e for e in (raw_tasks if isinstance(raw_tasks, list) else []) if isinstance(e, dict)]
    recorded = [_task_from_entry(entry) for entry in entries]

    duplicates = _duplicate_task_ids(recorded)
    if duplicates:
        # Re-deriving would raise here, and a verifier owes the caller a
        # verdict rather than a traceback: a receipt naming one task twice is
        # a receipt no honest run produced.
        return ReceiptVerification(
            status=RECEIPT_DIVERGED,
            divergences=(f"receipt names the same task more than once: {list(duplicates)}",),
        )

    divergences: list[str] = []
    rebuilt = recorded
    full = False

    if graph_document_bytes is not None:
        from bernstein.core.knowledge.ast_symbol_graph import graph_digest, graph_from_document
        from bernstein.core.knowledge.code_graph import SemanticCodeGraph, attribute_task

        try:
            graph = graph_from_document(graph_document_bytes)
        except ValueError as exc:
            # A document the loader refuses is not a divergence: nothing about
            # the recorded decision was contradicted, the operator was simply
            # handed something that is not the graph. Same next move as a
            # digest mismatch -- go and find the right one.
            return ReceiptVerification(
                status=RECEIPT_GRAPH_MISMATCH,
                divergences=(f"supplied graph document could not be loaded: {exc}",),
            )
        if graph_digest(graph) != graph_digest_value:
            return ReceiptVerification(
                status=RECEIPT_GRAPH_MISMATCH,
                divergences=("supplied graph document does not hash to the digest it was given",),
            )

        code_graph = SemanticCodeGraph(graph)
        rederived = [
            attribute_task(
                code_graph,
                task.task_id,
                task.declared_paths,
                depth=task.depth,
            )
            for task in recorded
        ]
        for was, now in zip(recorded, rederived, strict=True):
            if was != now:
                divergences.append(f"task {was.task_id!r} recorded {was.to_dict()!r}, re-derived {now.to_dict()!r}")
        rebuilt = rederived
        full = True

    expected = build_admission_receipt(graph_digest_value, rebuilt)
    # Every projected field, not only the verdicts. A receipt whose verdicts
    # survive an edit elsewhere -- a rewritten ``version``, a reordered or
    # padded ``tasks`` list -- is still a receipt that does not say what the
    # run produced, and comparing the whole canonical projection is what makes
    # "verified" mean the document reproduces byte for byte.
    for key in ("version", "tasks", "pairs", "serial_groups"):
        if receipt.get(key) != expected[key]:
            divergences.append(f"{key} recorded as {receipt.get(key)!r}, re-derived {expected[key]!r}")

    if divergences:
        return ReceiptVerification(status=RECEIPT_DIVERGED, divergences=tuple(sorted(divergences)))
    return ReceiptVerification(
        status=RECEIPT_VERIFIED if full else RECEIPT_CONSISTENT_ONLY,
        divergences=(),
    )


def _str_tuple(value: object) -> tuple[str, ...]:
    """Coerce a recorded field to a tuple of strings, tolerating absence."""
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value)


def _task_from_entry(entry: dict[str, object]) -> TaskNodeSet:
    """Rebuild one :class:`TaskNodeSet` from its recorded mapping.

    Every field is coerced rather than trusted: the receipt may have been
    edited, and a verifier that raises on a malformed value reports a crash
    where it should report a divergence.
    """
    raw_depth = entry.get("depth", 0)
    depth = raw_depth if isinstance(raw_depth, int) and not isinstance(raw_depth, bool) else 0
    return TaskNodeSet(
        task_id=str(entry.get("task_id", "")),
        declared_paths=_str_tuple(entry.get("declared_paths")),
        seed_symbols=_str_tuple(entry.get("seed_symbols")),
        neighborhood=_str_tuple(entry.get("neighborhood")),
        depth=depth,
        verdict=str(entry.get("verdict", "")),
        reasons=_str_tuple(entry.get("reasons")),
    )
