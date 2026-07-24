"""Figure tokenizer: the false-positive policy surface (issue #2888).

The tokenizer classifies every numeric token in a report body as *material*
(demands a grounding anchor) or *exempt* (section numbers, ISO dates, versions,
allowlisted patterns, incidental below-threshold counts). The policy is pinned
by an extensible vector file so a classification regression fails loudly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.tasks.figures import TokenizerPolicy, tokenize_numbers

_VECTORS = Path(__file__).parent / "data" / "figure_tokenizer_vectors.json"


def _load_vectors() -> tuple[TokenizerPolicy, list[dict]]:
    data = json.loads(_VECTORS.read_text(encoding="utf-8"))
    policy = TokenizerPolicy.from_dict(data.get("policy", {}))
    return policy, data["cases"]


_POLICY, _CASES = _load_vectors()


@pytest.mark.parametrize("case", _CASES, ids=[c["id"] for c in _CASES])
def test_tokenizer_vector(case: dict) -> None:
    tokens = tokenize_numbers(case["text"], _POLICY)
    got = [{"surface": t.surface, "material": t.material, "category": t.category} for t in tokens]
    assert got == case["expect"], f"case {case['id']!r}: {got} != {case['expect']}"


def test_material_categories_demand_anchors() -> None:
    """AC3: quantities, currency, percentages, and counts are material."""
    materials = {
        "$5": "currency",
        "12%": "percentage",
        "3.2 GB": "quantity",
        "1,234": "count",
    }
    for text, category in materials.items():
        tokens = tokenize_numbers(f"value {text} here", _POLICY)
        material = [t for t in tokens if t.material]
        assert material, f"{text!r} should yield a material token"
        assert material[0].category == category


def test_exempt_categories_do_not_demand_anchors() -> None:
    """AC3: section numbers, ISO dates, versions, allowlist stay exempt."""
    for text in ("§3.2", "Section 7", "2026-01-02", "v1.2.3", "9.8.7"):
        tokens = tokenize_numbers(f"ref {text} end", _POLICY)
        assert all(not t.material for t in tokens), f"{text!r} produced a material token"


def test_token_carries_location() -> None:
    tokens = tokenize_numbers("line one\nrevenue was $1,000 here", _POLICY)
    material = [t for t in tokens if t.material]
    assert len(material) == 1
    tok = material[0]
    assert tok.line == 2
    assert tok.col >= 1
    # The numeric core is exposed for value matching against a sidecar.
    assert tok.numeric_key == "1000"


def test_materiality_threshold_is_configurable() -> None:
    # With a low threshold, a bare small integer becomes material.
    low = TokenizerPolicy(materiality_min=1)
    tokens = tokenize_numbers("there are 7 shards", low)
    material = [t for t in tokens if t.material]
    assert len(material) == 1
    assert material[0].category == "count"


def test_numeric_key_normalises_grouping_and_trailing_zeros() -> None:
    from bernstein.core.tasks.figures import numeric_key_of

    assert numeric_key_of("1,234.00") == "1234"
    assert numeric_key_of("12.50") == "12.5"
    assert numeric_key_of("$1,000") == "1000"
    assert numeric_key_of("9.9%") == "9.9"
