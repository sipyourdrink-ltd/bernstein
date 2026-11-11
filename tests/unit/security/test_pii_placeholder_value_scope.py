"""Built-in key placeholders are fixture values, not database-name prefixes."""

from __future__ import annotations

from itertools import product
from typing import TYPE_CHECKING

import pytest

from bernstein.core.quality.quality_gates import QualityGatesConfig, _run_pii_gate
from bernstein.core.security.pii_output_gate import scan_diff, scan_text

if TYPE_CHECKING:
    from pathlib import Path


_KEY_MARKERS = [f"your{first}api{second}key" for first, second in product(("", "_", "-"), repeat=2)]
_URL_PREFIX = "postgres" + "://review-user:ReviewValue7@db.review.invalid:5432/"
_REAL_KEY = "q7V" + "_mP2xr7Tz4LwN9cR6"


@pytest.mark.parametrize("marker", _KEY_MARKERS)
@pytest.mark.parametrize("suffix", ["_db", "-2"])
def test_a_marker_prefixed_database_keeps_its_blocking_finding(marker: str, suffix: str) -> None:
    control = f'DATABASE_URL = "{_URL_PREFIX}app"'
    assert any(f.rule == "connection_string" and f.block_merge for f in scan_text(control))

    line = f'DATABASE_URL = "{_URL_PREFIX}{marker}{suffix}"'
    assert any(f.rule == "connection_string" and f.block_merge for f in scan_text(line))
    assert any(f.rule == "connection_string" and f.block_merge for f in scan_diff("+" + line + "\n"))


@pytest.mark.parametrize("database", ["your_api_key_db", "your-api-key-2"])
def test_a_placeholder_database_prefix_cannot_pass_the_changed_file_gate(tmp_path: Path, database: str) -> None:
    path = tmp_path / "settings.py"
    config = QualityGatesConfig(pii_scan=True)
    path.write_text(f'DATABASE_URL = "{_URL_PREFIX}app"\n', encoding="utf-8")
    control = _run_pii_gate(config, tmp_path, changed_files=[path.name])
    assert control.blocked and not control.passed

    path.write_text(f'DATABASE_URL = "{_URL_PREFIX}{database}"\n', encoding="utf-8")
    result = _run_pii_gate(config, tmp_path, changed_files=[path.name])
    assert result.blocked and not result.passed
    assert "connection_string" in result.detail


@pytest.mark.parametrize("marker", _KEY_MARKERS)
@pytest.mark.parametrize("prefixes", [None, ["ACME"]], ids=["defaults", "operator-prefixes"])
def test_builtin_key_placeholder_values_survive_operator_prefix_configuration(
    tmp_path: Path, marker: str, prefixes: list[str] | None
) -> None:
    line = f'api_key = "{marker}_here_review123"'
    assert not scan_text(line, allowlist_prefixes=prefixes)
    assert any(f.rule == "generic_api_key" for f in scan_text(f'api_key = "{_REAL_KEY}"', allowlist_prefixes=prefixes))

    path = tmp_path / "settings.py"
    path.write_text(line, encoding="utf-8")
    config = (
        QualityGatesConfig(pii_scan=True)
        if prefixes is None
        else QualityGatesConfig(pii_scan=True, pii_allowlist_prefixes=prefixes)
    )
    result = _run_pii_gate(config, tmp_path, changed_files=[path.name])
    assert result.passed and not result.blocked


@pytest.mark.parametrize("marker", _KEY_MARKERS)
def test_a_key_placeholder_value_does_not_hide_an_adjacent_credential(marker: str) -> None:
    line = f'api_key = "{marker}_here_review123"; DATABASE_URL = "{_URL_PREFIX}app"'
    assert any(f.rule == "connection_string" and f.block_merge for f in scan_text(line))
