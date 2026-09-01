"""Tests for deterministic task-tier classification (#4854)."""

from __future__ import annotations

import json
import subprocess
import sys

from hypothesis import given
from hypothesis import strategies as st

from bernstein.core.routing.task_tier import (
    FEATURE_ORDER,
    TIER_ERROR,
    TIER_POLICY_VERSION,
    TIERS,
    classify_from_artefacts,
    classify_tier,
    error_decision,
    extract_features,
    feature_digest,
    tier_for_score,
    verify_tier_decision,
)

_CLASSIFY_HELPER = """
import json, sys
from bernstein.core.routing.task_tier import classify_from_artefacts
payload = json.loads(sys.argv[1])
decision = classify_from_artefacts(
    labels=payload.get("labels"),
    paths=payload.get("paths"),
    symbol_nodes=payload.get("symbol_nodes"),
)
print(json.dumps(decision.to_record(), sort_keys=True))
"""


def test_feature_order_is_documented_and_stable() -> None:
    assert FEATURE_ORDER == (
        "size_rank",
        "file_count",
        "test_touched",
        "code_file_count",
        "symbol_nodes",
    )


def test_absent_surface_still_classifies() -> None:
    decision = classify_from_artefacts()
    assert decision.tier in TIERS
    assert decision.policy_version == TIER_POLICY_VERSION
    assert set(decision.features) == set(FEATURE_ORDER)


def test_size_label_and_files_raise_tier() -> None:
    light = classify_from_artefacts(labels=["size/xs"], paths=["README.md"])
    heavy = classify_from_artefacts(
        labels=["size/xl"],
        paths=[f"src/a{i}.py" for i in range(8)] + ["tests/unit/test_a.py"],
        symbol_nodes=40,
    )
    assert light.tier == "light"
    assert heavy.tier in {"heavy", "critical"}
    assert light.feature_digest != heavy.feature_digest


def test_boundary_ties_take_higher_band() -> None:
    assert tier_for_score(4) == "standard"
    assert tier_for_score(10) == "heavy"
    assert tier_for_score(18) == "critical"


def test_error_marker_is_outside_tier_set() -> None:
    assert TIER_ERROR not in TIERS
    decision = error_decision()
    assert decision.tier == TIER_ERROR


def test_verify_names_policy_version_divergence() -> None:
    decision = classify_from_artefacts(labels=["size/s"], paths=["a.py"])
    recorded = decision.to_record()
    recorded["tier_policy_version"] = TIER_POLICY_VERSION + 1
    reason = verify_tier_decision(recorded, labels=["size/s"], paths=["a.py"])
    assert reason is not None
    assert "tier_policy_version diverged" in reason


def test_determinism_across_processes() -> None:
    payload = {
        "labels": ["size/m", "area/core"],
        "paths": ["src/bernstein/core/foo.py", "tests/unit/test_foo.py"],
        "symbol_nodes": 12,
    }
    blob = json.dumps(payload)
    first = subprocess.check_output([sys.executable, "-c", _CLASSIFY_HELPER, blob], text=True)
    second = subprocess.check_output([sys.executable, "-c", _CLASSIFY_HELPER, blob], text=True)
    assert json.loads(first) == json.loads(second)
    assert json.loads(first)["tier"] in TIERS


@given(
    labels=st.lists(st.sampled_from(["size/xs", "size/s", "size/m", "size/l", "size/xl", "area/core", ""]), max_size=4),
    paths=st.lists(st.text(min_size=0, max_size=24), max_size=8),
    symbol_nodes=st.one_of(st.none(), st.integers(min_value=-5, max_value=200)),
)
def test_totality_every_input_yields_one_tier(
    labels: list[str],
    paths: list[str],
    symbol_nodes: int | None,
) -> None:
    decision = classify_from_artefacts(labels=labels, paths=paths, symbol_nodes=symbol_nodes)
    assert decision.tier in TIERS
    assert len(feature_digest(decision.features)) == 64


def test_digest_stable_for_equal_features() -> None:
    a = extract_features(labels=["size/s"], paths=["a.py"])
    b = extract_features(labels=["size/s"], paths=["a.py"])
    assert feature_digest(a.as_ordered_dict()) == feature_digest(b.as_ordered_dict())
    assert classify_tier(a) == classify_tier(b)
