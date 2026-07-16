"""Canary security-floor guard and last-green property (issue #2515).

The canary refuses to certify a below-floor upstream version and records the
refusal as a receipt; the last-green projection can never advance onto a
below-floor version.
"""

from __future__ import annotations

import stat
from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from bernstein.adapters.advisories import ADAPTER_MIN_SAFE_VERSIONS, check_adapter_version
from bernstein.adapters.canary import (
    CanaryOutcome,
    CanaryTarget,
    apply_canary_outcome,
    load_last_green,
    run_canary_target,
    update_last_green,
)

_GENERATED_AT = "2026-07-16T00:00:00Z"


def _write_version_stub(bin_dir: Path, name: str, *, version: str) -> Path:
    """Write an executable stub that answers ``--version`` with ``version``."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / name
    path.write_text(f'#!/bin/sh\necho "{name} {version}"\n', encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _outcome(adapter: str, *, verdict: str, version: str | None) -> CanaryOutcome:
    return CanaryOutcome(
        adapter=adapter,
        binary=adapter,
        model="m",
        goal="g",
        installed_version=version,
        verdict=verdict,
    )


# ---------------------------------------------------------------------------
# run_canary_target: below-floor is refused, not certified
# ---------------------------------------------------------------------------


class TestCanaryRefusesBelowFloor:
    def test_below_floor_version_is_refused(self, tmp_path: Path) -> None:
        # agy floor is 1.0.0; a 0.5.0 build must be refused before conformance.
        stub = _write_version_stub(tmp_path / "bin", "agy", version="0.5.0")
        target = CanaryTarget(adapter="agy", binary="agy", model="default")
        outcome = run_canary_target(target, which=lambda _n: str(stub), contracts_dir=tmp_path / "contracts")
        assert outcome.verdict == "refuse"
        assert outcome.refused_below_floor is True
        assert outcome.installed_version == "0.5.0"
        assert outcome.security_floor == ADAPTER_MIN_SAFE_VERSIONS["agy"].min_safe_version
        assert any("security floor" in f for f in outcome.failures)
        assert any(ADAPTER_MIN_SAFE_VERSIONS["agy"].advisory_id in f for f in outcome.failures)

    def test_refusal_does_not_open_a_regression_issue(self) -> None:
        outcome = _outcome("agy", verdict="refuse", version="0.5.0")
        _state, should_open = apply_canary_outcome({}, outcome)
        assert should_open is False


# ---------------------------------------------------------------------------
# Last-green guard: no below-floor row (AC: property test)
# ---------------------------------------------------------------------------


class TestLastGreenFloorGuard:
    def test_below_floor_pass_never_enters_last_green(self) -> None:
        # Even if presented as a pass, a below-floor version is rejected.
        outcome = _outcome("agy", verdict="pass", version="0.5.0")
        entries = update_last_green({}, outcome, receipt_sha="ab" * 32, recorded_at=_GENERATED_AT)
        assert "agy" not in entries

    def test_at_or_above_floor_pass_enters_last_green(self) -> None:
        outcome = _outcome("agy", verdict="pass", version="1.4.0")
        entries = update_last_green({}, outcome, receipt_sha="ab" * 32, recorded_at=_GENERATED_AT)
        assert entries["agy"].version == "1.4.0"

    @given(
        adapter=st.sampled_from(sorted(ADAPTER_MIN_SAFE_VERSIONS)),
        major=st.integers(min_value=0, max_value=5),
        minor=st.integers(min_value=0, max_value=99),
        patch=st.integers(min_value=0, max_value=99),
    )
    def test_property_no_last_green_row_below_floor(self, adapter: str, major: int, minor: int, patch: int) -> None:
        version = f"{major}.{minor}.{patch}"
        outcome = _outcome(adapter, verdict="pass", version=version)
        entries = update_last_green({}, outcome, receipt_sha="cd" * 32, recorded_at=_GENERATED_AT)
        # No row may reference a version below the adapter's floor.
        for name, entry in entries.items():
            assert check_adapter_version(name, entry.version) is None

    def test_packaged_last_green_has_no_below_floor_row(self) -> None:
        # The shipped projection itself honors the floor for tracked adapters.
        for name, entry in load_last_green().items():
            if name in ADAPTER_MIN_SAFE_VERSIONS:
                assert check_adapter_version(name, entry.version) is None, f"{name} {entry.version}"
