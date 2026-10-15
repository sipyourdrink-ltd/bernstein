"""``dlp_scan`` reads credential-shaped literals in argument and default position (#6152).

The line rules key on assignment shape, so a provider key passed to
``os.getenv`` as its default -- or to ``dict.get``, ``setdefault``, a
parameter default, a dataclass field -- passed every gate. These tests pin
the syntax-tree pass that closes it: which positions it reads, which values
it flags, and which it deliberately leaves alone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.quality.quality_gates import QualityGatesConfig, _is_dlp_test_path, _run_dlp_gate
from bernstein.core.security.dlp_scanner import DLPConfig, DLPScanner
from bernstein.eval.bench.gate_evasion_suite import _FINDING_SIGNATURE

#: Built at run time so this file carries no credential-shaped literal itself.
LIVE = "sk" + "-live-" + "12345-placeholder-secret-evasion"
STRIPE = "sk" + "_live_" + "4eC39HqLyjWDarjtT1zdp7dc"
AWS = "AKIA" + "Z3J7QX5M2KDWPL4R"
GITHUB = "ghp" + "_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


def _rules(source: str, config: DLPConfig | None = None) -> list[tuple[str, int]]:
    result = DLPScanner(config).scan_python_source(source)
    return [(f.rule, f.line_number) for f in result.findings]


@pytest.mark.parametrize(
    "source",
    [
        f"import os\nAPI_KEY = os.getenv('API_KEY', '{LIVE}')\n",
        f"import os\nAPI_KEY = os.environ.get('API_KEY', '{LIVE}')\n",
        f"def f(settings):\n    return settings.get('key', '{LIVE}')\n",
        f"def f(kwargs):\n    kwargs.setdefault('api_key', '{LIVE}')\n",
        f"def connect(api_key='{LIVE}'):\n    return api_key\n",
        f"def connect(*, api_key='{LIVE}'):\n    return api_key\n",
        f"connect = lambda api_key='{LIVE}': api_key\n",
        f"from dataclasses import dataclass\n\n@dataclass\nclass C:\n    api_key: str = '{LIVE}'\n",
        f"from dataclasses import dataclass, field\n\n@dataclass\nclass C:\n    api_key: str = field(default='{LIVE}')\n",
    ],
    ids=["getenv", "environ_get", "dict_get", "setdefault", "param", "kwonly", "lambda", "dataclass", "field"],
)
def test_a_live_key_in_argument_or_default_position_is_flagged(source: str) -> None:
    result = DLPScanner().scan_python_source(source)

    assert [f.rule for f in result.findings] == ["live_payment_key"]
    finding = result.findings[0]
    assert finding.category == "credential_literal"
    assert finding.block_merge is True
    assert LIVE not in finding.redacted_match
    assert "***" in finding.redacted_match


@pytest.mark.parametrize(
    ("value", "rule"),
    [(STRIPE, "live_payment_key"), (AWS, "aws_access_key_literal"), (GITHUB, "github_token_literal")],
)
def test_each_provider_shape_is_recognised(value: str, rule: str) -> None:
    assert _rules(f"x = os.getenv('K', '{value}')\n") == [(rule, 1)]


def test_a_plain_assignment_is_left_to_the_line_rules() -> None:
    """``pii_scan`` reads ``name = "..."``; this pass is for the positions it cannot see."""
    assert _rules(f"API_KEY = '{LIVE}'\n") == []


def test_a_live_prefix_is_not_excused_by_an_allowlist_word() -> None:
    """The fixture's value says "placeholder"; the provider reads the prefix."""
    assert "placeholder" in LIVE
    assert _rules(f"os.getenv('K', '{LIVE}')\n") == [("live_payment_key", 1)]


def test_an_unmarked_shape_is_excused_by_an_allowlist_word() -> None:
    """AWS's own documentation key ends in EXAMPLE and carries no live marker."""
    assert _rules("os.getenv('K', 'AKIAIOSFODNN7EXAMPLE')\n") == []


@pytest.mark.parametrize(
    "value",
    ["sk_live_xxxxxxxxxxxxxxxx", "sk-live-XXXXXXXX-XXXX", "AKIA0000000000000000", "FAKE-" + STRIPE],
    ids=["filler_x", "filler_upper", "filler_zero", "allowlisted_prefix"],
)
def test_filler_and_allowlisted_values_are_not_flagged(value: str) -> None:
    assert _rules(f"os.getenv('K', '{value}')\n") == []


def test_an_operator_allowlist_pattern_suppresses_the_line() -> None:
    config = DLPConfig(allowlist_patterns=[r"dlp:\s*ignore"])
    assert _rules(f"os.getenv('K', '{LIVE}')  # dlp: ignore\n", config) == []


def test_the_pass_can_be_turned_off_or_downgraded() -> None:
    source = f"os.getenv('K', '{LIVE}')\n"
    assert _rules(source, DLPConfig(check_credential_literals=False)) == []

    warned = DLPScanner(DLPConfig(block_credential_literals=False)).scan_python_source(source)
    assert [f.block_merge for f in warned.findings] == [False]
    assert warned.has_blocks is False


def test_a_file_that_does_not_parse_yields_nothing() -> None:
    assert _rules(f"def f(:\n    os.getenv('K', '{LIVE}')\n") == []


@pytest.mark.parametrize(
    ("rel", "is_test"),
    [
        ("tests/unit/test_x.py", True),
        ("pkg/tests/helpers.py", True),
        ("src/test_payments.py", True),
        ("src/payments_test.py", True),
        ("conftest.py", True),
        ("src/config.py", False),
        ("src/contest.py", False),
        ("tests.py", False),
    ],
)
def test_test_path_rule(rel: str, is_test: bool) -> None:
    assert _is_dlp_test_path(rel) is is_test


def _write(root: Path, rel: str, text: str) -> str:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return rel


def test_the_gate_blocks_and_names_the_file(tmp_path: Path) -> None:
    rel = _write(tmp_path, "app/config.py", f"import os\n\nAPI_KEY = os.getenv('API_KEY', '{LIVE}')\n")

    result = _run_dlp_gate(QualityGatesConfig(), tmp_path, [rel])

    assert result.blocked is True
    assert result.passed is False
    assert "app/config.py (line 3)" in result.detail
    assert "credential_literal" in result.detail
    assert LIVE not in result.detail
    # The evasion suite credits the gate only through this signature.
    assert _FINDING_SIGNATURE["dlp_scan"].search(result.detail)


def test_the_gate_skips_test_files_for_this_pass(tmp_path: Path) -> None:
    rel = _write(tmp_path, "tests/test_scanner.py", f"def test_x(scan):\n    assert scan('{LIVE}')\n")

    result = _run_dlp_gate(QualityGatesConfig(), tmp_path, [rel])

    assert result.passed is True


def test_the_gate_config_switch_reaches_the_scanner(tmp_path: Path) -> None:
    rel = _write(tmp_path, "config.py", f"import os\nAPI_KEY = os.getenv('API_KEY', '{LIVE}')\n")

    result = _run_dlp_gate(QualityGatesConfig(dlp_block_credential_literals=False), tmp_path, [rel])

    assert result.passed is True
    assert "[WARN]" in result.detail
    assert not _FINDING_SIGNATURE["dlp_scan"].search(result.detail)
