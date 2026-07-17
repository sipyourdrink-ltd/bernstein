"""Unit tests for the cache-policy engine (issue #2551).

Covers the policy model, the composable key recipe, and the mandatory-ingredient
key discrimination (AC6): any change to a mandatory ingredient (model version,
adapter version, base worktree commit, tool schema hash) or a declared optional
ingredient produces a different composed key.
"""

from __future__ import annotations

import dataclasses

import pytest

from bernstein.core.persistence.cache_policy import (
    MANDATORY_INGREDIENTS,
    CachePolicy,
    RecipeInputs,
    compose_key_hex,
    compose_recipe,
    recipe_hash,
)


def _inputs(**over: object) -> RecipeInputs:
    base = {
        "model_id": "claude-opus-4-8",
        "model_version": "2026-01-15",
        "adapter_version": "claude@1.4.2",
        "base_commit": "a" * 40,
        "tool_schema_hash": "sha256:tools",
        "task_inputs": {"goal": "add login endpoint"},
        "producer_code": "def run(): ...",
        "run_parameters": {"temperature": 0.0},
    }
    base.update(over)
    return RecipeInputs(**base)  # type: ignore[arg-type]


def test_policy_hash_is_deterministic_and_stable() -> None:
    a = CachePolicy(ingredients=("task_inputs",), expiry_mode="drift", drift_window=3)
    b = CachePolicy(ingredients=("task_inputs",), expiry_mode="drift", drift_window=3)
    assert a.policy_hash() == b.policy_hash()
    assert a.policy_hash().startswith("sha256:")


def test_policy_roundtrip_from_dict() -> None:
    policy = CachePolicy(
        ingredients=("task_inputs", "producer_code"),
        expiry_mode="both",
        drift_window=5,
        ttl_seconds=3600,
        verified_only=True,
        world_facing=True,
        store_scope="research",
    )
    restored = CachePolicy.from_dict(policy.to_dict())
    assert restored == policy
    assert restored.policy_hash() == policy.policy_hash()


def test_policy_rejects_unknown_ingredient() -> None:
    with pytest.raises(ValueError, match="unknown optional ingredient"):
        CachePolicy(ingredients=("not_a_real_ingredient",))


def test_policy_rejects_duplicate_ingredient() -> None:
    with pytest.raises(ValueError, match="duplicate ingredient"):
        CachePolicy(ingredients=("task_inputs", "task_inputs"))


def test_policy_ttl_required_when_mode_includes_ttl() -> None:
    with pytest.raises(ValueError, match="ttl_seconds must be a positive int"):
        CachePolicy(expiry_mode="ttl")
    with pytest.raises(ValueError, match="does not include a TTL backstop"):
        CachePolicy(expiry_mode="drift", ttl_seconds=60)


def test_recipe_lists_mandatory_ingredients_first() -> None:
    policy = CachePolicy(ingredients=("task_inputs",))
    recipe = compose_recipe(policy, _inputs())
    names = [ing["name"] for ing in recipe["ingredients"]]
    assert names[: len(MANDATORY_INGREDIENTS)] == list(MANDATORY_INGREDIENTS)
    assert names[-1] == "task_inputs"
    assert recipe["policy_hash"] == policy.policy_hash()


def test_compose_key_is_deterministic() -> None:
    policy = CachePolicy(ingredients=("task_inputs",))
    assert compose_key_hex(policy, _inputs()) == compose_key_hex(policy, _inputs())


@pytest.mark.parametrize("ingredient", list(MANDATORY_INGREDIENTS))
def test_mandatory_ingredient_change_changes_key(ingredient: str) -> None:
    policy = CachePolicy(ingredients=("task_inputs",))
    baseline = compose_key_hex(policy, _inputs())
    mutated = compose_key_hex(policy, _inputs(**{ingredient: "DIFFERENT-" + ingredient}))
    assert mutated != baseline, f"changing mandatory ingredient {ingredient} must change the key"


def test_declared_optional_ingredient_change_changes_key() -> None:
    policy = CachePolicy(ingredients=("task_inputs",))
    baseline = compose_key_hex(policy, _inputs())
    mutated = compose_key_hex(policy, _inputs(task_inputs={"goal": "something else"}))
    assert mutated != baseline


def test_undeclared_ingredient_does_not_affect_key() -> None:
    # producer_code is not declared, so changing it must not change the key.
    policy = CachePolicy(ingredients=("task_inputs",))
    baseline = compose_key_hex(policy, _inputs())
    unchanged = compose_key_hex(policy, _inputs(producer_code="def run(): return 42"))
    assert unchanged == baseline


def test_policy_hash_bound_into_key() -> None:
    # Two policies over identical inputs must produce distinct keys because the
    # policy hash is folded into the recipe.
    p1 = CachePolicy(ingredients=("task_inputs",), store_scope="a")
    p2 = CachePolicy(ingredients=("task_inputs",), store_scope="b")
    assert compose_key_hex(p1, _inputs()) != compose_key_hex(p2, _inputs())


def test_recipe_hash_matches_key_derivation() -> None:
    policy = CachePolicy(ingredients=("task_inputs",))
    recipe = compose_recipe(policy, _inputs())
    # recipe_hash is a stable sha256 over the recipe; identical recipes agree.
    assert recipe_hash(recipe) == recipe_hash(compose_recipe(policy, _inputs()))


def test_frozen_policy_is_hashable() -> None:
    policy = CachePolicy(ingredients=("task_inputs",))
    assert dataclasses.is_dataclass(policy)
    # Frozen dataclasses participate in sets - useful as dict keys downstream.
    assert {policy, policy} == {policy}
