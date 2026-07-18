"""Cache policy engine: composable key recipes and drift-based expiry.

Bernstein historically carried three result caches, each hard-coding its own
key recipe (``fingerprint`` folds the producer AST, ``action_cache`` keys the
model-visible inputs, ``semantic_cache`` keys an exact SHA-256 of normalised
text) with no shared vocabulary. None captured model version, adapter version,
base worktree commit, or tool schema hash, so a cached agent output could
silently survive a model upgrade, an adapter bump, or a tool-schema change; and
the only staleness signal was the wall clock, so a result whose producing diff
touched files that have since changed was still served.

This module defines what a cache policy *means* at the cache boundary:

* :class:`CachePolicy` names an ordered set of optional key ingredients on top
  of the five mandatory ingredients (model id, model version, adapter version,
  base worktree commit, tool schema hash). The policy canonicalises to JSON
  with a ``sha256`` policy hash.
* :func:`compose_recipe` / :func:`compose_key` fold the mandatory ingredients
  plus the declared optional ones into one canonical recipe whose ``sha256`` is
  the recipe hash. The composed key is the ``sha256`` of that recipe, so any
  change to a mandatory ingredient changes the key (the whole point).
* :class:`CacheEntry` is a content-addressed lineage record: it pins the input
  hashes, the output hash, the producing task, the recorded diff file set with
  per-file content hashes, and the base commit.
* :func:`evaluate_freshness` is a *pure* verdict function over ``(entry,
  policy, repo_state)``. It reads no clock, no filesystem outside the injected
  repo state, and no network; two operators with the same repo state compute
  the byte-identical verdict JSON instead of racing a clock. A wall-clock TTL
  applies only as a declared backstop for world-facing tasks, and only through
  an injected timestamp.

Determinism substrate: every serialised artefact here (policy JSON, recipe,
composed key, freshness verdict) is canonical JSON - sorted keys, minimal
separators, UTF-8 - so two byte-identical inputs produce byte-identical bytes
and hashes across processes and platforms.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Literal, cast

if TYPE_CHECKING:
    from bernstein.core.tasks.models import Task

# ---------------------------------------------------------------------------
# Ingredient vocabulary
# ---------------------------------------------------------------------------

#: Ingredients folded into every composed key regardless of policy. A change to
#: any one of these produces a different key - a cached output never survives a
#: model, adapter, repo-base, or tool-surface change it did not account for.
MANDATORY_INGREDIENTS: Final[tuple[str, ...]] = (
    "model_id",
    "model_version",
    "adapter_version",
    "base_commit",
    "tool_schema_hash",
)

#: Optional ingredients a policy may add to the recipe, in the caller's order.
OPTIONAL_INGREDIENTS: Final[tuple[str, ...]] = (
    "task_inputs",
    "producer_code",
    "run_parameters",
)

ExpiryMode = Literal["drift", "ttl", "both"]

_EXPIRY_MODES: Final[frozenset[str]] = frozenset({"drift", "ttl", "both"})

_DIGEST_PREFIX = "sha256:"


def _canonical_bytes(value: Any) -> bytes:
    """Return canonical JSON bytes (sorted keys, minimal separators, UTF-8)."""
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256_hex(data: bytes) -> str:
    """Return the ``sha256:``-prefixed hex digest of ``data``."""
    return _DIGEST_PREFIX + hashlib.sha256(data).hexdigest()


#: Environment signal set by ``bernstein run --refresh-cache``. When present the
#: cache boundary treats every policy lookup as a miss for that run and then
#: repopulates with the fresh output.
_REFRESH_ENV = "BERNSTEIN_REFRESH_CACHE"


def refresh_requested(env: Mapping[str, str] | None = None) -> bool:
    """Return whether ``--refresh-cache`` requested a policy cache bypass.

    Reads ``BERNSTEIN_REFRESH_CACHE`` from ``env`` (defaulting to the process
    environment). A truthy value (``"1"``/``"true"``/``"yes"``) forces every
    policy lookup to miss for the run so the cache repopulates with fresh
    outputs. (issue #2551)
    """
    source = os.environ if env is None else env
    return str(source.get(_REFRESH_ENV, "")).strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# CachePolicy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CachePolicy:
    """A declared cache policy attached to a task spec.

    Attributes:
        ingredients: Ordered optional key ingredients drawn from
            :data:`OPTIONAL_INGREDIENTS`. Duplicates and unknown names are
            rejected at construction. The mandatory ingredients are always
            folded in and are never listed here.
        expiry_mode: ``"drift"`` (repo-state verdict only), ``"ttl"``
            (wall-clock backstop only), or ``"both"``.
        drift_window: Number of commits the base commit may be behind the
            current head and still count as fresh. ``0`` means the base must be
            the head (distance 0). Must be ``>= 0``.
        ttl_seconds: Wall-clock backstop, in seconds. Required (and only
            honoured) when ``expiry_mode`` includes ttl. ``None`` otherwise.
        verified_only: When true, only outputs whose evidence bundle passed the
            completion gate are cacheable; the semantic cache's ``verified``
            flag is promoted to a policy term.
        world_facing: Marks a task whose result depends on external state (web
            research, third-party API). Only a world-facing policy honours the
            TTL backstop; a repo-local task's freshness is a pure function of
            repo state with no clock.
        store_scope: Namespace label so distinct policies never share a key
            namespace in the backing store.
    """

    ingredients: tuple[str, ...] = ()
    expiry_mode: ExpiryMode = "drift"
    drift_window: int = 0
    ttl_seconds: int | None = None
    verified_only: bool = False
    world_facing: bool = False
    store_scope: str = "default"

    def __post_init__(self) -> None:
        """Validate the policy invariants once at construction."""
        object.__setattr__(self, "ingredients", tuple(self.ingredients))
        seen: set[str] = set()
        for name in self.ingredients:
            if name not in OPTIONAL_INGREDIENTS:
                raise ValueError(
                    f"CachePolicy.ingredients: unknown optional ingredient {name!r}; "
                    f"choose from {sorted(OPTIONAL_INGREDIENTS)}"
                )
            if name in seen:
                raise ValueError(f"CachePolicy.ingredients: duplicate ingredient {name!r}")
            seen.add(name)
        if self.expiry_mode not in _EXPIRY_MODES:
            raise ValueError(
                f"CachePolicy.expiry_mode must be one of {sorted(_EXPIRY_MODES)}, got {self.expiry_mode!r}"
            )
        if self.drift_window < 0:
            raise ValueError(f"CachePolicy.drift_window must be >= 0, got {self.drift_window!r}")
        ttl_active = self.expiry_mode in ("ttl", "both")
        if ttl_active and (self.ttl_seconds is None or self.ttl_seconds <= 0):
            raise ValueError(
                f"CachePolicy.ttl_seconds must be a positive int when expiry_mode={self.expiry_mode!r}, "
                f"got {self.ttl_seconds!r}"
            )
        if not ttl_active and self.ttl_seconds is not None:
            raise ValueError(
                f"CachePolicy.ttl_seconds is set ({self.ttl_seconds!r}) but expiry_mode={self.expiry_mode!r} "
                "does not include a TTL backstop"
            )

    def canonical(self) -> dict[str, Any]:
        """Return the canonical dict form used for hashing and serialisation."""
        return {
            "ingredients": list(self.ingredients),
            "expiry_mode": self.expiry_mode,
            "drift_window": self.drift_window,
            "ttl_seconds": self.ttl_seconds,
            "verified_only": self.verified_only,
            "world_facing": self.world_facing,
            "store_scope": self.store_scope,
        }

    def canonical_json(self) -> bytes:
        """Return canonical JSON bytes of the policy."""
        return _canonical_bytes(self.canonical())

    def policy_hash(self) -> str:
        """Return the ``sha256:`` digest of the canonical policy bytes."""
        return _sha256_hex(self.canonical_json())

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation (alias of canonical)."""
        return self.canonical()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CachePolicy:
        """Build a :class:`CachePolicy` from a JSON-decoded mapping.

        Unknown keys are ignored; missing keys fall back to dataclass defaults.
        The result is validated by :meth:`__post_init__`.
        """
        raw_ingredients: object = data.get("ingredients") or []
        if isinstance(raw_ingredients, (str, bytes)):
            raise TypeError("CachePolicy.ingredients must be a list of ingredient names, not a scalar")
        if not isinstance(raw_ingredients, (list, tuple)):
            raise TypeError(
                f"CachePolicy.ingredients must be a list of ingredient names, got {type(raw_ingredients).__name__}"
            )
        names: tuple[str, ...] = tuple(str(name) for name in cast("Iterable[Any]", raw_ingredients))
        ttl = data.get("ttl_seconds")
        return cls(
            ingredients=names,
            expiry_mode=str(data.get("expiry_mode", "drift")),  # type: ignore[arg-type]
            drift_window=int(data.get("drift_window", 0)),
            ttl_seconds=None if ttl is None else int(ttl),
            verified_only=bool(data.get("verified_only", False)),
            world_facing=bool(data.get("world_facing", False)),
            store_scope=str(data.get("store_scope", "default")),
        )

    @classmethod
    def from_task(cls, task: Task) -> CachePolicy | None:
        """Return the policy declared on ``task``, or ``None`` when absent.

        A task declares its policy on the ``cache_policy`` spec field. A task
        that never opted in returns ``None`` and runs byte-identically to the
        pre-policy spawn path.
        """
        declared = getattr(task, "cache_policy", None)
        if declared is None:
            return None
        if isinstance(declared, CachePolicy):
            return declared
        if isinstance(declared, Mapping):
            return cls.from_dict(cast("Mapping[str, Any]", declared))
        raise TypeError(f"task.cache_policy must be a CachePolicy or mapping, got {type(declared).__name__}")


# ---------------------------------------------------------------------------
# Recipe composition / key engine
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecipeInputs:
    """The concrete ingredient values a run resolves for one cache lookup.

    The five mandatory fields source from the adapter contract (adapter and
    model identity), :func:`bernstein.core.git.git_basic.rev_parse_head` (base
    commit), and the task's declared tool surface (tool schema hash). The three
    optional fields are folded only when the policy names them.
    """

    model_id: str
    model_version: str
    adapter_version: str
    base_commit: str
    tool_schema_hash: str
    task_inputs: Any = None
    producer_code: str | None = None
    run_parameters: Mapping[str, Any] | None = None

    def value_for(self, ingredient: str) -> Any:
        """Return the resolved value for ``ingredient`` (mandatory or optional)."""
        return getattr(self, ingredient)


def compose_recipe(policy: CachePolicy, inputs: RecipeInputs) -> dict[str, Any]:
    """Return the ordered ingredient recipe for ``policy`` over ``inputs``.

    The recipe lists the mandatory ingredients first, in their canonical order,
    then the policy's declared optional ingredients in the policy's order. Each
    entry hashes its value to a ``sha256:`` digest so the recipe never embeds a
    raw prompt or diff, only content-addressed anchors. The policy hash is bound
    into the recipe so two policies over identical inputs still produce distinct
    keys.
    """
    ordered: list[dict[str, str]] = []
    for name in MANDATORY_INGREDIENTS:
        ordered.append({"name": name, "hash": _sha256_hex(_canonical_bytes(inputs.value_for(name)))})
    for name in policy.ingredients:
        ordered.append({"name": name, "hash": _sha256_hex(_canonical_bytes(inputs.value_for(name)))})
    return {
        "v": 1,
        "policy_hash": policy.policy_hash(),
        "store_scope": policy.store_scope,
        "ingredients": ordered,
    }


def recipe_hash(recipe: Mapping[str, Any]) -> str:
    """Return the ``sha256:`` digest of the canonical recipe bytes."""
    return _sha256_hex(_canonical_bytes(dict(recipe)))


def compose_key(policy: CachePolicy, inputs: RecipeInputs) -> bytes:
    """Return the 32-byte composed cache key digest for ``policy``/``inputs``.

    The digest is ``sha256(canonical(recipe))`` as raw bytes, suitable as a
    :class:`bernstein.core.persistence.fingerprint.MemoStore` key. Any change to
    a mandatory or declared ingredient alters the recipe and therefore the key.
    """
    recipe = compose_recipe(policy, inputs)
    return hashlib.sha256(_canonical_bytes(recipe)).digest()


def compose_key_hex(policy: CachePolicy, inputs: RecipeInputs) -> str:
    """Return the composed key as a plain hex string (no prefix)."""
    return compose_key(policy, inputs).hex()


# ---------------------------------------------------------------------------
# Cache entry (content-addressed lineage record)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CacheEntry:
    """A content-addressed cache entry pinning provenance for one output.

    Attributes:
        key: The composed cache key (hex).
        input_hashes: Per-input ``sha256:`` anchors (name -> digest).
        output_hash: ``sha256:`` digest of the cached output bytes.
        producing_task: Task id that produced the output.
        diff_file_hashes: Repo-relative path -> ``sha256:`` content hash of the
            file *as it was when the output was produced*. Drift expiry compares
            these against current repo state.
        base_commit: The worktree head commit the output was produced against.
        verified: Whether the output's evidence bundle passed the completion
            gate; a ``verified_only`` policy refuses to serve unverified entries.
        recipe_hash: The recipe hash the key was composed from.
        policy_hash: The policy hash in force at production.
        created_ts: Optional production timestamp; only a world-facing TTL
            policy reads it, and only through an injected ``now`` value.
    """

    key: str
    input_hashes: dict[str, str]
    output_hash: str
    producing_task: str
    diff_file_hashes: dict[str, str]
    base_commit: str
    verified: bool = False
    recipe_hash: str = ""
    policy_hash: str = ""
    created_ts: int | None = None

    def canonical(self) -> dict[str, Any]:
        """Return the canonical dict form of the entry."""
        return {
            "key": self.key,
            "input_hashes": self.input_hashes.copy(),
            "output_hash": self.output_hash,
            "producing_task": self.producing_task,
            "diff_file_hashes": self.diff_file_hashes.copy(),
            "base_commit": self.base_commit,
            "verified": self.verified,
            "recipe_hash": self.recipe_hash,
            "policy_hash": self.policy_hash,
            "created_ts": self.created_ts,
        }

    def content_id(self) -> str:
        """Return the ``sha256:`` digest of the entry's canonical bytes."""
        return _sha256_hex(_canonical_bytes(self.canonical()))

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation (alias of canonical)."""
        return self.canonical()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CacheEntry:
        """Reconstruct a :class:`CacheEntry` from its JSON dict form."""
        created = data.get("created_ts")
        return cls(
            key=str(data["key"]),
            input_hashes={str(k): str(v) for k, v in dict(data.get("input_hashes") or {}).items()},
            output_hash=str(data["output_hash"]),
            producing_task=str(data.get("producing_task", "")),
            diff_file_hashes={str(k): str(v) for k, v in dict(data.get("diff_file_hashes") or {}).items()},
            base_commit=str(data.get("base_commit", "")),
            verified=bool(data.get("verified", False)),
            recipe_hash=str(data.get("recipe_hash", "")),
            policy_hash=str(data.get("policy_hash", "")),
            created_ts=None if created is None else int(created),
        )


# ---------------------------------------------------------------------------
# Drift-based freshness verdict (pure function)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RepoState:
    """The injected repo state a freshness verdict is computed against.

    The verdict function reads only this object - never the live clock,
    filesystem, or network - so two operators who assemble the same
    ``RepoState`` compute byte-identical verdicts.

    Attributes:
        file_hashes: Current ``sha256:`` content hash for each path the entry's
            diff touched. A path absent from this mapping is treated as deleted
            (drift).
        ancestor_distance: How many commits the entry's ``base_commit`` is
            behind the current head along the first-parent chain, or ``None``
            when the base is not an ancestor of the head (rebased away).
        now: Injected timestamp in seconds. Consumed only by a world-facing TTL
            policy; ``None`` disables the TTL branch entirely.
    """

    file_hashes: dict[str, str] = field(default_factory=dict[str, str])
    ancestor_distance: int | None = None
    now: int | None = None


@dataclass(frozen=True)
class FreshnessVerdict:
    """The deterministic outcome of :func:`evaluate_freshness`.

    ``reason`` is a stable, machine-readable token (never free prose) so two
    processes emit byte-identical verdict JSON. ``detail`` carries the offending
    path or distance when relevant.
    """

    fresh: bool
    reason: str
    detail: str = ""

    def canonical(self) -> dict[str, Any]:
        """Return the canonical dict form of the verdict."""
        return {"fresh": self.fresh, "reason": self.reason, "detail": self.detail}

    def canonical_json(self) -> bytes:
        """Return canonical JSON bytes of the verdict."""
        return _canonical_bytes(self.canonical())


# Verdict reason tokens (stable machine-readable vocabulary).
REASON_FRESH = "fresh"
REASON_FILE_DRIFT = "file_drift"
REASON_FILE_DELETED = "file_deleted"
REASON_BASE_NOT_ANCESTOR = "base_not_ancestor"
REASON_BASE_OUTSIDE_WINDOW = "base_outside_window"
REASON_TTL_EXPIRED = "ttl_expired"


def evaluate_freshness(
    entry: CacheEntry,
    policy: CachePolicy,
    repo_state: RepoState,
) -> FreshnessVerdict:
    """Return the freshness verdict for ``entry`` under ``policy``.

    Pure function of ``(entry, policy, repo_state)``: reads no clock, no
    filesystem outside ``repo_state``, and no network. The drift branch fires
    first (repo-local truth is authoritative), then the world-facing TTL
    backstop. Checks run in a fixed, sorted order so the *first* failing check
    is deterministic across processes.

    Determinism contract (issue #2551 AC1): two processes given the same
    ``entry`` and ``repo_state`` produce the byte-identical
    :meth:`FreshnessVerdict.canonical_json`.
    """
    drift_active = policy.expiry_mode in ("drift", "both")
    ttl_active = policy.expiry_mode in ("ttl", "both")

    if drift_active:
        # 1. Per-file content drift, evaluated in sorted path order so the
        #    first offending file is a deterministic choice.
        for path in sorted(entry.diff_file_hashes):
            recorded = entry.diff_file_hashes[path]
            current = repo_state.file_hashes.get(path)
            if current is None:
                return FreshnessVerdict(fresh=False, reason=REASON_FILE_DELETED, detail=path)
            if current != recorded:
                return FreshnessVerdict(fresh=False, reason=REASON_FILE_DRIFT, detail=path)
        # 2. Base commit ancestry / window.
        distance = repo_state.ancestor_distance
        if distance is None:
            return FreshnessVerdict(fresh=False, reason=REASON_BASE_NOT_ANCESTOR, detail=entry.base_commit)
        if distance > policy.drift_window:
            return FreshnessVerdict(
                fresh=False,
                reason=REASON_BASE_OUTSIDE_WINDOW,
                detail=str(distance),
            )

    if ttl_active and policy.world_facing:
        # TTL backstop applies only to declared world-facing tasks and only
        # when both timestamps are present; otherwise the branch is inert so a
        # repo-local verdict never reads a clock.
        now = repo_state.now
        created = entry.created_ts
        if now is not None and created is not None and policy.ttl_seconds is not None:
            age = now - created
            if age > policy.ttl_seconds:
                return FreshnessVerdict(fresh=False, reason=REASON_TTL_EXPIRED, detail=str(age))

    return FreshnessVerdict(fresh=True, reason=REASON_FRESH)


__all__ = [
    "MANDATORY_INGREDIENTS",
    "OPTIONAL_INGREDIENTS",
    "REASON_BASE_NOT_ANCESTOR",
    "REASON_BASE_OUTSIDE_WINDOW",
    "REASON_FILE_DELETED",
    "REASON_FILE_DRIFT",
    "REASON_FRESH",
    "REASON_TTL_EXPIRED",
    "CacheEntry",
    "CachePolicy",
    "ExpiryMode",
    "FreshnessVerdict",
    "RecipeInputs",
    "RepoState",
    "compose_key",
    "compose_key_hex",
    "compose_recipe",
    "evaluate_freshness",
    "recipe_hash",
    "refresh_requested",
]
