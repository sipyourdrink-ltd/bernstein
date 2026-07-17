"""Deterministic projection from a schedule fire to a canonical task graph.

The projection is the load-bearing property of #1798: two operators with
identical ``(schedule_id, fire_time, last_state)`` MUST land on the
byte-identical task graph. Any drift breaks the reproducible-firing
contract that downstream audit walks depend on.

Discipline (do not relax without parent approval):

- The projection function is pure. No ``time.time()``, no wall-clock
  comparisons, no random shuffling, no host-dependent ordering, no
  network reads, no environment lookups.
- All inputs flow in via the function signature; all outputs flow out via
  the returned task-graph mapping.
- The canonical encoding sorts keys and freezes container order so two
  dicts that compare equal serialise to byte-identical JSON.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Mapping

#: Schema-rev marker baked into the projection. Bumping the rev changes
#: every projection_hash and is therefore the single source of truth for
#: when the deterministic contract is allowed to evolve.
SCHEDULE_PROJECTION_REV = "1"


@dataclass(frozen=True)
class TaskNode:
    """One node of the projected task graph.

    Frozen + sortable so the canonical encoder can lay nodes out by a
    stable key regardless of how the projection iterated through state.
    """

    task_id: str
    role: str
    title: str
    description: str
    depends_on: tuple[str, ...] = ()
    metadata: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ProjectionResult:
    """The byte-stable output of the projection function.

    Attributes:
        nodes: Tuple of task nodes ordered by ``task_id``.
        projection_hash: SHA-256 over the canonical bytes; this is the
            value an operator records in the audit chain and compares
            against a second operator's chain to prove they fired the
            byte-identical graph.
        canonical_bytes: The exact bytes hashed; surfacing them lets the
            audit-chain verifier do its own digest re-check without
            recomputing the projection.
    """

    nodes: tuple[TaskNode, ...]
    projection_hash: str
    canonical_bytes: bytes
    rev: str = SCHEDULE_PROJECTION_REV
    schedule_id: str = ""
    fire_time: int = 0
    last_state_digest: str = ""
    recurrence: str = ""
    trigger_input_hash: str = ""
    params_hash: str = ""

    @property
    def graph_hash(self) -> str:
        """Alias for :attr:`projection_hash` in the #2302 vocabulary.

        The issue frames a fire as "a hash rather than a trigger": the
        canonical task graph's ``graph_hash`` is what ``schedule show
        --at`` prints and ``schedule verify`` re-derives. It is the same
        value as :attr:`projection_hash`; the alias keeps the projection's
        historical name stable while giving callers the graph-centric name
        the CLI and journal use.
        """
        return self.projection_hash

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe view of the projection.

        Useful for ``schedule show --json`` and for the audit-chain
        payload. The dict is built from the canonical bytes so the JSON
        round-trips identically across runs.
        """
        return json.loads(self.canonical_bytes.decode())


def _canonicalize_last_state(value: Any) -> Any:
    """Recursively normalise *value* into an order-stable, drift-proof form.

    The deterministic contract requires that two operators with logically
    equal ``last_state`` mappings land on byte-identical bytes. Plain
    ``json.dumps(..., sort_keys=True)`` does NOT achieve this for the
    full ``Any`` value space:

    - ``set`` / ``frozenset`` members are not JSON-serialisable at all
      (``json.dumps`` raises), and even when pre-converted their member
      order is hash-randomised per ``PYTHONHASHSEED``.
    - ``float`` members serialise with a platform/version-dependent
      repr, and ``NaN``/``Infinity`` round-trip through a non-portable
      ``json`` extension token rather than valid JSON.

    Sets/frozensets are folded into a sorted ``["__set__", [...]]``
    sentinel (the same canonical shape as
    :func:`bernstein.core.persistence.fingerprint._canonicalize`, so the
    two subsystems cannot diverge); dicts and sequences recurse so nested
    sets are also normalised.

    ``float`` is rejected with a ``TypeError`` mirroring the ``fire_time``
    float guard rather than silently producing a host-dependent digest.
    A caller that genuinely needs a real-valued component must quantise
    or stringify it deterministically before folding it into the state.
    """
    if isinstance(value, bool):
        # ``bool`` is an ``int`` subclass but JSON-stable; keep it as-is
        # (and intercept before the float/int paths so it is not coerced).
        return value
    if isinstance(value, float):
        raise TypeError(
            "last_state must not contain a float; floats serialise with a "
            "host-dependent repr and permit cross-host drift (mirrors the "
            "fire_time float guard). Quantise or stringify deterministically."
        )
    if isinstance(value, dict):
        mapping = cast("dict[Any, Any]", value)
        items: list[tuple[Any, Any]] = sorted(mapping.items(), key=lambda kv: repr(kv[0]))
        return {str(k): _canonicalize_last_state(v) for k, v in items}
    if isinstance(value, (set, frozenset)):
        container = cast("set[Any] | frozenset[Any]", value)
        members: list[Any] = sorted((_canonicalize_last_state(v) for v in container), key=repr)
        return ["__set__", members]
    if isinstance(value, (list, tuple)):
        sequence = cast("list[Any] | tuple[Any, ...]", value)
        return [_canonicalize_last_state(v) for v in sequence]
    return value


def _digest_last_state(last_state: Mapping[str, Any] | None) -> str:
    """Hash the ``last_state`` mapping into a stable digest.

    A None / empty mapping hashes to a fixed sentinel so the very first
    fire of a fresh schedule produces a deterministic projection without
    requiring the caller to invent a synthetic state.

    Non-empty mappings are canonicalised by :func:`_canonicalize_last_state`
    before hashing so set member order, nested-container order, and dict
    key order do not perturb the digest, and so non-portable scalar types
    (``float``/``NaN``/``Infinity``) are rejected rather than silently
    forking two operators' projections.
    """
    if not last_state:
        return "genesis"
    canonical = json.dumps(
        _canonicalize_last_state(dict(last_state)),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(canonical).hexdigest()[:16]


def _canonical_nodes(nodes: list[TaskNode]) -> tuple[TaskNode, ...]:
    """Sort task nodes by id so projection output is order-independent.

    The projection callers may emit nodes in any order; the canonical
    output sorts by ``task_id``. Two operators that disagree on
    iteration order still converge on the same byte sequence.
    """
    return tuple(sorted(nodes, key=lambda n: n.task_id))


def _node_to_dict(node: TaskNode) -> dict[str, Any]:
    """Serialise a TaskNode to a sort-friendly dict.

    ``depends_on`` and ``metadata`` are sorted explicitly so a caller
    that emits ``("b", "a")`` lands on the same bytes as a caller that
    emits ``("a", "b")``.
    """
    return {
        "task_id": node.task_id,
        "role": node.role,
        "title": node.title,
        "description": node.description,
        "depends_on": sorted(node.depends_on),
        "metadata": sorted([list(item) for item in node.metadata]),
    }


def project_schedule_fire(
    *,
    schedule_id: str,
    fire_time: int,
    last_state: Mapping[str, Any] | None,
    goal: str = "",
    scenario_id: str = "",
    response_profile: str = "",
    profile_content_sha256: str = "",
    recurrence: str = "",
    trigger_input_hash: str = "",
    params_hash: str = "",
    timezone: str = "",
    dst_policy: str = "",
) -> ProjectionResult:
    """Project ``(schedule_id, fire_time, last_state)`` onto a task graph.

    PURE function. No wall-clock, no randomness, no host-dependent state.

    The projection currently emits a single root task that the
    orchestrator picks up via the existing trigger pipeline. Future revs
    may emit multi-node graphs; bump ``SCHEDULE_PROJECTION_REV`` when
    that happens, because the projection_hash baked into past audit
    entries must remain reproducible against the rev that produced them.

    Args:
        schedule_id: Stable schedule identifier.
        fire_time: Unix epoch (integer seconds) of the canonical fire
            instant. We deliberately require ``int`` not ``float`` so
            sub-second drift cannot fork two operators' projections.
        last_state: Optional mapping that callers can use to fold the
            previous fire outcome into the projection. Today this is
            unused in the body but baked into the digest so the
            contract is honoured by the function signature.
        goal: Free-form goal text from the registered schedule.
        scenario_id: Optional named scenario id.
        response_profile: Optional response-style profile declared on the
            schedule. When non-empty it is folded
            into the task identity seed and the canonical payload, so the
            profile is an input to the task hash rather than a logged
            field. Empty (the default, and the state of every pre-change
            schedule) keeps the seed, payload, and hash byte-identical to
            prior revs - the fold is deliberately conditional so replay of
            existing audit chains is unaffected.
        profile_content_sha256: SHA-256 of the rendered style addendum the
            profile resolves to; folded alongside ``response_profile``.
        recurrence: Canonical recurrence rule (RFC-5545 ``RRULE:`` or
            ``cron:`` form, see
            :func:`bernstein.core.orchestration.recurrence.canonicalise_recurrence`)
            that produced this fire instant. When non-empty it is folded
            into the task identity and canonical payload so two operators
            prove they fired under the byte-identical schedule definition.
            Empty (the default, and every pre-#2302 schedule) keeps the
            seed, payload, and hash byte-identical to prior revs.
        trigger_input_hash: For a webhook or file-change trigger, the
            SHA-256 of the trigger event that woke the projection. Folded
            in ONLY when non-empty so a scheduled (cron/RRULE) fire with no
            external trigger stays byte-identical to prior revs, while a
            trigger-driven fire binds the exact event bytes into the graph
            hash - the same projection, with the trigger as an input hash.
        params_hash: For a parameterized fire (#2545), the ``sha256:`` content
            hash of the validated parameter map
            (:meth:`bernstein.core.tasks.param_contract.ParamContract.params_hash`).
            Folded in ONLY when non-empty, following the ``trigger_input_hash``
            precedent, so a params-less fire stays byte-identical to prior revs
            while a parameterized fire binds the exact validated inputs into the
            task identity and the graph hash. Two operators with equal schedule
            state and params provably fire the byte-identical graph; a changed
            param provably changes the graph hash.
        timezone: Declared IANA timezone the schedule evaluates its local
            wall time in (#2546). Folded into the task identity and payload
            ONLY when non-empty, so two hosts in different system timezones
            prove the identical fire decision through a DST transition,
            while a UTC / zone-less schedule (every pre-#2546 schedule)
            stays byte-identical to prior revs.
        dst_policy: The DST ambiguity policy (see
            :class:`bernstein.core.orchestration.schedule_kinds.DstPolicy`)
            that resolved this fire instant; folded alongside ``timezone``
            so the ambiguity decision is part of the canonical body rather
            than an out-of-band host default.

    Returns:
        A ProjectionResult with the canonical task graph and its hash.
    """
    # Runtime guard against an untyped caller handing us a float: even
    # though the type signature pins ``int``, the deterministic contract
    # is load-bearing enough that we re-check at runtime. The pyright
    # ``reportUnnecessaryIsInstance`` suppression is deliberate.
    if not isinstance(fire_time, int):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise TypeError("fire_time must be an integer epoch second; floats permit cross-host drift")
    state_digest = _digest_last_state(last_state)

    # Build a single root task. The description is fully determined by
    # the inputs so two operators land on byte-identical output.
    description_lines = [
        f"Schedule {schedule_id} fire at epoch {fire_time}.",
    ]
    if goal:
        description_lines.append(f"Goal: {goal}")
    if scenario_id:
        description_lines.append(f"Scenario: {scenario_id}")
    description = "\n".join(description_lines)

    # The task_id MUST be deterministic. Derive it from the canonical
    # tuple so two operators recompute the same id. The response profile
    # is folded in ONLY when explicitly declared: adding the keys
    # unconditionally would fork the task identity of every profile-less
    # schedule against previously recorded audit chains.
    task_id_seed_obj: dict[str, Any] = {
        "schedule_id": schedule_id,
        "fire_time": fire_time,
        "state_digest": state_digest,
        "kind": "root",
        "rev": SCHEDULE_PROJECTION_REV,
    }
    if response_profile:
        task_id_seed_obj["response_profile"] = response_profile
        task_id_seed_obj["profile_content_sha256"] = profile_content_sha256
    if recurrence:
        task_id_seed_obj["recurrence"] = recurrence
    if trigger_input_hash:
        task_id_seed_obj["trigger_input_hash"] = trigger_input_hash
    if params_hash:
        task_id_seed_obj["params_hash"] = params_hash
    if timezone:
        task_id_seed_obj["timezone"] = timezone
        task_id_seed_obj["dst_policy"] = dst_policy
    task_id_seed = json.dumps(task_id_seed_obj, sort_keys=True).encode()
    task_id = "sched-task-" + hashlib.sha256(task_id_seed).hexdigest()[:16]

    metadata: tuple[tuple[str, str], ...] = (
        ("schedule_id", schedule_id),
        ("fire_time", str(fire_time)),
        ("rev", SCHEDULE_PROJECTION_REV),
        ("source", "schedule"),
    )
    if scenario_id:
        metadata = (*metadata, ("scenario_id", scenario_id))
    if recurrence:
        metadata = (*metadata, ("recurrence", recurrence))
    if trigger_input_hash:
        metadata = (*metadata, ("trigger_input_hash", trigger_input_hash))
    if params_hash:
        metadata = (*metadata, ("params_hash", params_hash))
    if timezone:
        metadata = (*metadata, ("timezone", timezone), ("dst_policy", dst_policy))
    if response_profile:
        # ``mode`` rides the node metadata so a task created from this node
        # resolves the same profile at spawn time
        # (``Task.metadata['mode']`` is the top-priority resolution input).
        metadata = (
            *metadata,
            ("mode", response_profile),
            ("response_profile", response_profile),
            ("profile_content_sha256", profile_content_sha256),
        )

    role = "manager"
    title = f"Scheduled goal: {(goal or scenario_id or schedule_id)[:120]}"

    root = TaskNode(
        task_id=task_id,
        role=role,
        title=title,
        description=description,
        depends_on=(),
        metadata=metadata,
    )

    nodes = _canonical_nodes([root])
    canonical_obj: dict[str, Any] = {
        "rev": SCHEDULE_PROJECTION_REV,
        "schedule_id": schedule_id,
        "fire_time": fire_time,
        "last_state_digest": state_digest,
        "goal": goal,
        "scenario_id": scenario_id,
        "nodes": [_node_to_dict(n) for n in nodes],
    }
    if response_profile:
        canonical_obj["response_profile"] = response_profile
        canonical_obj["profile_content_sha256"] = profile_content_sha256
    if recurrence:
        canonical_obj["recurrence"] = recurrence
    if trigger_input_hash:
        canonical_obj["trigger_input_hash"] = trigger_input_hash
    if params_hash:
        canonical_obj["params_hash"] = params_hash
    if timezone:
        canonical_obj["timezone"] = timezone
        canonical_obj["dst_policy"] = dst_policy
    canonical_bytes = json.dumps(
        canonical_obj,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    projection_hash = hashlib.sha256(canonical_bytes).hexdigest()

    return ProjectionResult(
        nodes=nodes,
        projection_hash=projection_hash,
        canonical_bytes=canonical_bytes,
        rev=SCHEDULE_PROJECTION_REV,
        schedule_id=schedule_id,
        fire_time=fire_time,
        last_state_digest=state_digest,
        recurrence=recurrence,
        trigger_input_hash=trigger_input_hash,
        params_hash=params_hash,
    )


def project(
    schedule_id: str,
    fire_time: int,
    last_state: Mapping[str, Any] | None,
    *,
    goal: str = "",
    scenario_id: str = "",
    recurrence: str = "",
    trigger_event: bytes | None = None,
    params_hash: str = "",
) -> ProjectionResult:
    """Project ``(schedule_id, fire_time, last_state)`` onto a task graph.

    The #2302 entrypoint: a recurring goal fire is a pure function of its
    inputs onto a canonical task graph with a deterministic ``graph_hash``.
    Two calls with identical ``(schedule_id, fire_time, last_state)`` (and
    identical ``recurrence`` / ``trigger_event``) return byte-identical
    graphs and hashes - the load-bearing determinism the issue proves.

    This is a thin, ergonomic front for :func:`project_schedule_fire`:

    - ``recurrence`` is canonicalised (RFC-5545 ``RRULE`` or ``cron``)
      before folding, so two operators who wrote the same rule in a
      different token order still converge on the same ``graph_hash``.
    - ``trigger_event`` (webhook / file-change payload bytes) is hashed
      and folded in as ``trigger_input_hash``, so a trigger-driven fire is
      *the same projection* with the trigger event bound in by hash. A
      cron/RRULE fire passes ``trigger_event=None`` and stays
      byte-identical to a pre-#2302 projection.

    Args:
        schedule_id: Stable schedule identifier.
        fire_time: Integer Unix epoch of the canonical fire instant.
        last_state: Optional mapping folded into the projection digest.
        goal: Free-form goal text.
        scenario_id: Optional named scenario id.
        recurrence: Recurrence rule text (bare cron, ``cron:...``, or
            ``RRULE:...``); empty means none declared.
        trigger_event: For a webhook / file-change trigger, the raw event
            bytes. ``None`` for a plain scheduled fire.

    Returns:
        A :class:`ProjectionResult`; ``result.graph_hash`` is the value an
        operator records and a second operator re-derives.

    Raises:
        RecurrenceParseError: When ``recurrence`` is non-empty and cannot
            be parsed.
        TypeError: When ``fire_time`` is not an ``int``.
    """
    from bernstein.core.orchestration.recurrence import canonicalise_recurrence

    canonical_recurrence = canonicalise_recurrence(recurrence) if recurrence else ""
    trigger_input_hash = "sha256:" + hashlib.sha256(trigger_event).hexdigest() if trigger_event is not None else ""
    return project_schedule_fire(
        schedule_id=schedule_id,
        fire_time=fire_time,
        last_state=last_state,
        goal=goal,
        scenario_id=scenario_id,
        recurrence=canonical_recurrence,
        trigger_input_hash=trigger_input_hash,
        params_hash=params_hash,
    )
