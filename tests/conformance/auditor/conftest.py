"""Fixtures for the auditor conformance suite."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conformance.auditor import offline, recorder
from tests.conformance.auditor.bundle import BundleReader


def pytest_configure(config: pytest.Config) -> None:
    """Register the marker that ties a vector to the question it answers."""
    config.addinivalue_line(
        "markers",
        "question(number): the auditor question this vector answers (1-21).",
    )


@pytest.fixture(scope="session")
def fixture_root() -> Path:
    """The committed recording: ``bundle/`` plus the out-of-band ``trust/``."""
    root = recorder.REPO_ROOT / recorder.FIXTURE_RELATIVE_PATH
    if not root.is_dir():
        pytest.fail(
            f"the recorded fixture is missing at {root}; "
            "regenerate it with 'uv run python scripts/auditor_conformance.py regenerate'"
        )
    return root


@pytest.fixture(scope="session")
def bundle_reader(fixture_root: Path) -> BundleReader:
    """Bundle-only access to the recorded export - all a vector may read."""
    return BundleReader.open(fixture_root / recorder.BUNDLE_DIR_NAME)


@pytest.fixture(scope="session")
def trust_anchor(fixture_root: Path) -> Path:
    """The operator's public key, held outside the bundle as an auditor holds it."""
    return fixture_root / recorder.TRUST_DIR_NAME / recorder.OPERATOR_PUBLIC_KEY_NAME


@pytest.fixture(scope="session")
def auditor_env(tmp_path_factory: pytest.TempPathFactory) -> offline.AuditorEnvironment:
    """An interpreter environment with the standalone verifier but no bernstein."""
    workdir = tmp_path_factory.mktemp("auditor-env")
    try:
        return offline.build_environment(workdir)
    except offline.AuditorEnvironmentError as exc:  # pragma: no cover - platform dependent
        pytest.fail(str(exc))
