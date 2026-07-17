"""Property test: pool placement is deterministic across override permutations.

Acceptance criterion (determinism): two hosts holding the same pool manifest and
the same recipe produce byte-identical effective manifests, and their placement
receipts agree on (pool_hash, effective_manifest_hash, chosen_backend) -- proven
here across a property space of override permutations.
"""

from __future__ import annotations

import hypothesis.strategies as st
from hypothesis import given, settings

from bernstein.core.sandbox.backend import SandboxCapability
from bernstein.core.sandbox.pool import PoolManifest, PoolWorkspaceTemplate, merge_pool_overrides
from bernstein.core.sandbox.pool_placement import seal_placement

KNOWN_CREDS = frozenset({"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"})


def _pool() -> PoolManifest:
    return PoolManifest(
        name="ci-linux",
        backend_allowlist=("worktree", "docker"),
        template=PoolWorkspaceTemplate(env={"FOO": "bar"}, timeout_seconds=900),
        exposed_fields=("env", "timeout_seconds", "network_egress_class", "capabilities"),
        capability_ceiling=frozenset({SandboxCapability.FILE_RW, SandboxCapability.EXEC, SandboxCapability.NETWORK}),
        network_egress_class="restricted",
        credential_env_allowlist=frozenset({"AWS_ACCESS_KEY_ID"}),
    )


_env_names = st.sampled_from(["A", "B", "C", "AWS_ACCESS_KEY_ID", "PATHX"])
_egress = st.sampled_from(["none", "loopback", "restricted"])  # never wider than ceiling
_caps = st.lists(st.sampled_from(["exec", "file_rw", "network"]), unique=True, max_size=3)


@st.composite
def _valid_overrides(draw):
    ov: dict = {}
    if draw(st.booleans()):
        env = draw(st.dictionaries(_env_names, st.text(min_size=0, max_size=5), max_size=4))
        ov["env"] = env
    if draw(st.booleans()):
        ov["timeout_seconds"] = draw(st.integers(min_value=1, max_value=7200))
    if draw(st.booleans()):
        ov["network_egress_class"] = draw(_egress)
    if draw(st.booleans()):
        ov["capabilities"] = draw(_caps)
    return ov


def _shuffle_mapping(mapping: dict, order: list[int]) -> dict:
    """Rebuild *mapping* iterating its items in a permuted order."""
    items = list(mapping.items())
    permuted = [items[i % len(items)] for i in order] if items else []
    out: dict = {}
    for key, value in permuted:
        out[key] = value
    for key, value in items:  # ensure completeness regardless of permutation
        out.setdefault(key, value)
    return out


@settings(max_examples=150, deadline=None)
@given(overrides=_valid_overrides(), perm=st.lists(st.integers(min_value=0, max_value=8), max_size=8))
def test_effective_manifest_hash_is_permutation_invariant(overrides, perm):
    pool = _pool()
    a = merge_pool_overrides(pool, overrides, known_credential_keys=KNOWN_CREDS)

    reordered = dict(overrides)
    if "env" in reordered and isinstance(reordered["env"], dict) and reordered["env"]:
        reordered["env"] = _shuffle_mapping(reordered["env"], perm or [0])
    # Rebuild the top-level dict in reverse key order too.
    reordered = {k: reordered[k] for k in reversed(list(reordered))}
    b = merge_pool_overrides(pool, reordered, known_credential_keys=KNOWN_CREDS)

    assert a.effective_manifest_hash == b.effective_manifest_hash
    assert a.overrides_hash == b.overrides_hash

    ra = seal_placement(merge=a, chosen_backend="worktree", selector_inputs={"c": ["worktree"]}, timestamp=42)
    rb = seal_placement(merge=b, chosen_backend="worktree", selector_inputs={"c": ["worktree"]}, timestamp=42)
    assert (ra.pool_hash, ra.effective_manifest_hash, ra.chosen_backend) == (
        rb.pool_hash,
        rb.effective_manifest_hash,
        rb.chosen_backend,
    )
    assert ra.placement_hash == rb.placement_hash
