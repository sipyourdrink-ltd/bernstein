"""Property tests for capability-token caveat subset semantics (issue #2611).

The soundness of attenuation rests on the caveat subset relation being exact:
if a child could ever widen its parent and still be accepted, a compromised
sub-agent could self-escalate. Two relations are property-tested here:

1. POSIX path-prefix coverage (``_path_covered_by``): reflexive, antisymmetric
   up to normalization, and immune to ``/a/bc`` vs ``/a/b`` prefix confusion.
2. The mint-time subset gate: :func:`attenuate` succeeds for a randomized
   ``(parent, child)`` caveat pair iff ``child.is_narrowing_of(parent)``.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from bernstein.core.security import capability_tokens as ct
from bernstein.core.security.agent_card_signer import generate_ed25519_keypair

# Fixed keypairs so each hypothesis example signs without regenerating keys and
# the attenuate signer-continuity check (issuer key == parent.subject_pubkey)
# always holds - keeping the test focused on the subset relation, not crypto.
_PRINCIPAL_PRIV, _PRINCIPAL_PUB = generate_ed25519_keypair()
_ORCH_PRIV, _ORCH_PUB = generate_ed25519_keypair()
_LEAF_PRIV, _LEAF_PUB = generate_ed25519_keypair()

_PERM_VOCAB = sorted(
    {
        "tasks:read",
        "tasks:write",
        "tasks:claim",
        "files:read",
        "files:write",
        "tests:run",
        "status:read",
        "agents:spawn",
    }
)

_NOW = 1_800_000_000.0

# ---------------------------------------------------------------------------
# Path-prefix coverage relation
# ---------------------------------------------------------------------------

_COMPONENT = st.text(st.characters(min_codepoint=ord("a"), max_codepoint=ord("z")), min_size=1, max_size=4)


@st.composite
def _abs_paths(draw: st.DrawFn) -> str:
    comps = draw(st.lists(_COMPONENT, min_size=1, max_size=4))
    return "/" + "/".join(comps)


@given(path=_abs_paths())
def test_path_coverage_is_reflexive(path: str) -> None:
    """Every path is an ancestor-or-equal of itself."""
    assert ct._path_covered_by(path, path)


@given(a=_abs_paths(), b=_abs_paths())
def test_path_coverage_antisymmetric_up_to_normalization(a: str, b: str) -> None:
    """Mutual coverage implies the paths normalize to the same thing."""
    if ct._path_covered_by(a, b) and ct._path_covered_by(b, a):
        assert ct._normalize_path(a) == ct._normalize_path(b)


@given(parent=_abs_paths(), suffix=_COMPONENT)
def test_path_coverage_rejects_component_boundary_confusion(parent: str, suffix: str) -> None:
    """``/a/b`` must not cover ``/a/bX`` - coverage is component-wise.

    A true child (``parent/suffix``) is covered; a sibling formed by extending
    the last component (``parent`` + ``suffix``, no separator) is not.
    """
    child = parent + "/" + suffix
    sibling = parent + suffix  # shares the string prefix but crosses no '/'
    assert ct._path_covered_by(child, parent)
    assert not ct._path_covered_by(sibling, parent)


@given(parent=_abs_paths())
def test_root_covers_everything(parent: str) -> None:
    """The filesystem root ``/`` is an ancestor-or-equal of any absolute path."""
    assert ct._path_covered_by(parent, "/")


# ---------------------------------------------------------------------------
# Randomized caveats and the mint-time subset gate
# ---------------------------------------------------------------------------


@st.composite
def _caveats(draw: st.DrawFn, *, min_depth: int, max_depth: int) -> ct.Caveats:
    permissions = frozenset(draw(st.lists(st.sampled_from(_PERM_VOCAB), max_size=len(_PERM_VOCAB), unique=True)))
    task_ids = draw(
        st.one_of(
            st.none(),
            st.lists(st.sampled_from(["t1", "t2", "t3", "t4"]), max_size=4, unique=True).map(frozenset),
        )
    )
    path_prefixes = draw(
        st.one_of(
            st.none(),
            st.lists(st.sampled_from(["/repo", "/repo/src", "/repo/tests", "/tmp"]), max_size=3, unique=True).map(
                frozenset
            ),
        )
    )
    not_after = draw(st.sampled_from([_NOW + 100, _NOW + 200, _NOW + 300]))
    max_uses = draw(st.one_of(st.none(), st.integers(min_value=1, max_value=10)))
    remaining_depth = draw(st.integers(min_value=min_depth, max_value=max_depth))
    return ct.Caveats(
        permissions=permissions,
        remaining_depth=remaining_depth,
        not_after=not_after,
        task_ids=task_ids,
        path_prefixes=path_prefixes,
        max_uses=max_uses,
    )


@settings(deadline=None, max_examples=250)
@given(
    parent=_caveats(min_depth=0, max_depth=5),
    child=_caveats(min_depth=-1, max_depth=6),
)
def test_attenuate_succeeds_iff_child_narrows_parent(parent: ct.Caveats, child: ct.Caveats) -> None:
    """The mint gate accepts a child iff its caveats are a subset of the parent."""
    root = ct.mint_root(
        issuer_identity_id="principal",
        issuer_private_key=_PRINCIPAL_PRIV,
        subject_identity_id="orchestrator",
        subject_pubkey=_ORCH_PUB,
        caveats=parent,
    )
    expected = child.is_narrowing_of(parent)
    try:
        ct.attenuate(
            root,
            issuer_private_key=_ORCH_PRIV,
            subject_identity_id="leaf",
            subject_pubkey=_LEAF_PUB,
            caveats=child,
        )
        minted = True
    except ct.AttenuationError:
        minted = False
    assert minted == expected


@settings(deadline=None, max_examples=100)
@given(parent=_caveats(min_depth=1, max_depth=5))
def test_narrowing_relation_is_reflexive_except_depth(parent: ct.Caveats) -> None:
    """A caveat set narrows itself on every axis except the strict-depth rule.

    ``is_narrowing_of`` requires ``remaining_depth`` strictly below the parent,
    so a set never narrows an identical copy, but a copy with depth reduced by
    one does.
    """
    assert not parent.is_narrowing_of(parent)
    import dataclasses

    one_deeper = dataclasses.replace(parent, remaining_depth=parent.remaining_depth - 1)
    assert one_deeper.is_narrowing_of(parent)
