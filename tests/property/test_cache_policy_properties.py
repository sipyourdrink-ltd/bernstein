"""Property tests for cache-policy recipe and verdict determinism (issue #2551).

* Recipe / key determinism (AC1, AC6): the composed key is a pure function of
  ``(policy, inputs)``; dict-ordering and hash-seed noise never change it, and
  any mandatory-ingredient change discriminates the key.
* Verdict determinism (AC1): two evaluations of the same ``(entry, policy,
  repo_state)`` produce byte-identical verdict JSON.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from bernstein.core.persistence.cache_policy import (
    MANDATORY_INGREDIENTS,
    CacheEntry,
    CachePolicy,
    RecipeInputs,
    RepoState,
    compose_key_hex,
    evaluate_freshness,
)

_hashes = st.text(min_size=1, max_size=24)
_json_scalar = st.one_of(st.text(max_size=20), st.integers(), st.booleans(), st.none())


def _inputs_from(values: dict[str, str], task_inputs: object) -> RecipeInputs:
    return RecipeInputs(
        model_id=values["model_id"],
        model_version=values["model_version"],
        adapter_version=values["adapter_version"],
        base_commit=values["base_commit"],
        tool_schema_hash=values["tool_schema_hash"],
        task_inputs=task_inputs,
    )


@given(
    values=st.fixed_dictionaries({name: _hashes for name in MANDATORY_INGREDIENTS}),
    task_inputs=st.dictionaries(st.text(max_size=8), _json_scalar, max_size=4),
)
def test_compose_key_is_pure_function(values: dict[str, str], task_inputs: dict[str, object]) -> None:
    policy = CachePolicy(ingredients=("task_inputs",))
    k1 = compose_key_hex(policy, _inputs_from(values, dict(task_inputs)))
    # Rebuild the dict in reversed insertion order - canonicalisation must erase
    # the ordering difference.
    reordered = dict(reversed(list(task_inputs.items())))
    k2 = compose_key_hex(policy, _inputs_from(values, reordered))
    assert k1 == k2


@given(
    values=st.fixed_dictionaries({name: _hashes for name in MANDATORY_INGREDIENTS}),
    ingredient=st.sampled_from(MANDATORY_INGREDIENTS),
    delta=st.text(min_size=1, max_size=8),
)
def test_mandatory_ingredient_change_discriminates(
    values: dict[str, str],
    ingredient: str,
    delta: str,
) -> None:
    policy = CachePolicy(ingredients=("task_inputs",))
    baseline = compose_key_hex(policy, _inputs_from(values, {"g": "x"}))
    mutated_values = dict(values)
    mutated_values[ingredient] = values[ingredient] + "|" + delta
    mutated = compose_key_hex(policy, _inputs_from(mutated_values, {"g": "x"}))
    assert mutated != baseline


@given(
    file_hashes=st.dictionaries(st.text(min_size=1, max_size=8), _hashes, max_size=4),
    distance=st.one_of(st.none(), st.integers(min_value=0, max_value=20)),
    window=st.integers(min_value=0, max_value=10),
)
def test_verdict_json_is_deterministic(
    file_hashes: dict[str, str],
    distance: int | None,
    window: int,
) -> None:
    policy = CachePolicy(expiry_mode="drift", drift_window=window)
    entry = CacheEntry(
        key="k",
        input_hashes={},
        output_hash="sha256:o",
        producing_task="t",
        diff_file_hashes=dict(file_hashes),
        base_commit="c" * 40,
    )
    state = RepoState(file_hashes=dict(file_hashes), ancestor_distance=distance)
    v1 = evaluate_freshness(entry, policy, state).canonical_json()
    v2 = evaluate_freshness(entry, policy, state).canonical_json()
    assert v1 == v2
