"""Property tests for content-addressed recipe hashing (#2546, AC1).

Over generated manifests:

- registration is deterministic: encoding the same manifest with the same
  pins twice yields byte-identical canonical bytes and the same hash;
- changing any pinned input (git commit, adapter, model, prompt pack)
  changes the hash.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from bernstein.core.workflows.recipe_registry import (
    RecipePins,
    compute_recipe_registration,
)
from bernstein.core.workflows.recipe_spec import RecipeSpec

_slug = st.from_regex(r"[a-z][a-z0-9_]{0,15}", fullmatch=True)
_text = st.text(
    alphabet=st.characters(min_codepoint=97, max_codepoint=122),
    min_size=1,
    max_size=40,
)
_hashish = st.text(alphabet="0123456789abcdef", min_size=1, max_size=40)


def _build_spec(name: str, description: str, command: str) -> RecipeSpec:
    # Build via model_validate rather than a YAML string so a generated
    # description that happens to be a YAML-reserved token (``no``, ``null``)
    # is treated as literal text - the hashing contract is what we test here.
    return RecipeSpec.model_validate(
        {
            "name": name,
            "description": description,
            "version": "1.0.0",
            "nodes": [{"id": "only", "command": command}],
        },
    )


@st.composite
def _pins(draw: st.DrawFn) -> RecipePins:
    return RecipePins(
        git_commit=draw(_hashish),
        adapter=draw(_slug),
        model=draw(_slug),
        prompt_pack_sha256=draw(_hashish),
    )


@given(name=_slug, description=_text, command=_text, pins=_pins())
def test_registration_is_deterministic(name: str, description: str, command: str, pins: RecipePins) -> None:
    spec = _build_spec(name, description, command)
    b1, h1, *_ = compute_recipe_registration(spec, pins=pins)
    b2, h2, *_ = compute_recipe_registration(spec, pins=pins)
    assert b1 == b2
    assert h1 == h2


@given(
    name=_slug,
    description=_text,
    command=_text,
    pins=_pins(),
    new_commit=_hashish,
)
def test_changing_git_commit_changes_hash(
    name: str,
    description: str,
    command: str,
    pins: RecipePins,
    new_commit: str,
) -> None:
    spec = _build_spec(name, description, command)
    _, base_hash, *_ = compute_recipe_registration(spec, pins=pins)
    other = RecipePins(
        git_commit=new_commit,
        adapter=pins.adapter,
        model=pins.model,
        prompt_pack_sha256=pins.prompt_pack_sha256,
    )
    _, other_hash, *_ = compute_recipe_registration(spec, pins=other)
    if new_commit != pins.git_commit:
        assert base_hash != other_hash
    else:
        assert base_hash == other_hash


@given(name=_slug, description=_text, command=_text, pins=_pins(), new_model=_slug)
def test_changing_model_changes_hash(
    name: str,
    description: str,
    command: str,
    pins: RecipePins,
    new_model: str,
) -> None:
    spec = _build_spec(name, description, command)
    _, base_hash, *_ = compute_recipe_registration(spec, pins=pins)
    other = RecipePins(
        git_commit=pins.git_commit,
        adapter=pins.adapter,
        model=new_model,
        prompt_pack_sha256=pins.prompt_pack_sha256,
    )
    _, other_hash, *_ = compute_recipe_registration(spec, pins=other)
    if new_model != pins.model:
        assert base_hash != other_hash
    else:
        assert base_hash == other_hash
