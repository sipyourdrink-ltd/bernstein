"""Config-boundary tests for the ``admission:`` block (#4907).

A typo in the block must fail the run at config load, naming the
offending key, rather than surfacing later as a refused spawn. Both the
seed parser (the runtime path) and the Pydantic schema (the
``load_and_validate`` path) must agree on what the block accepts.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.config.config_schema import load_and_validate
from bernstein.core.config.seed_config import SeedError
from bernstein.core.config.seed_parser import parse_seed

_GOAL = "goal: ship the thing\n"


def _seed(tmp_path: Path, body: str) -> Path:
    """Write a bernstein.yaml carrying *body* and return its path."""
    path = tmp_path / "bernstein.yaml"
    path.write_text(_GOAL + body, encoding="utf-8")
    return path


_VALID = (
    "admission:\n"
    "  mode: enforce\n"
    "  rules:\n"
    "    - id: approved-adapters\n"
    "      effect: allow\n"
    "      adapters: [claude, codex]\n"
    "      models: ['claude-*']\n"
    "    - id: no-unsandboxed\n"
    "      effect: deny\n"
    "      sandboxes: [none]\n"
)


class TestSeedParser:
    """``parse_seed`` is the runtime path; it owns the actionable error."""

    def test_valid_block_parses_into_an_evaluable_policy(self, tmp_path: Path) -> None:
        seed = parse_seed(_seed(tmp_path, _VALID))

        assert seed.admission is not None
        assert [rule.rule_id for rule in seed.admission.rules] == [
            "approved-adapters",
            "no-unsandboxed",
        ]

    def test_absent_block_leaves_the_policy_unset(self, tmp_path: Path) -> None:
        assert parse_seed(_seed(tmp_path, "")).admission is None

    def test_unknown_rule_key_is_rejected_with_the_key_named(self, tmp_path: Path) -> None:
        body = "admission:\n  rules:\n    - id: r\n      effect: allow\n      providers: [claude]\n"

        with pytest.raises(SeedError, match="unknown keys: providers"):
            parse_seed(_seed(tmp_path, body))

    def test_unknown_block_key_is_rejected_with_the_key_named(self, tmp_path: Path) -> None:
        body = "admission:\n  enforcement: enforce\n  rules: []\n"

        with pytest.raises(SeedError, match="unknown keys: enforcement"):
            parse_seed(_seed(tmp_path, body))


class TestPydanticSchema:
    """``load_and_validate`` must accept and reject exactly the same shapes."""

    def test_valid_block_validates(self, tmp_path: Path) -> None:
        config = load_and_validate(_seed(tmp_path, _VALID))

        assert config.admission is not None
        assert config.admission.mode == "enforce"
        assert config.admission.rules[1].effect == "deny"

    def test_unknown_rule_key_fails_validation(self, tmp_path: Path) -> None:
        body = "admission:\n  rules:\n    - id: r\n      effect: allow\n      providers: [claude]\n"

        with pytest.raises(ValueError, match="providers"):
            load_and_validate(_seed(tmp_path, body))
