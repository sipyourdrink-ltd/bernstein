"""Shared fixtures for the sandbox unit tests.

The microVM snapshot format is extracted through the stdlib tarfile ``data``
filter, and :func:`extract_workspace_image` refuses to run on a CPython build
whose filter predates the CVE-2025-4517-family fix (``<3.12.11`` / ``<3.13.4``).
That production guard is correct, but it would otherwise make every
round-trip / correctness test (conformance snapshot-resume, the canonical-image
round-trip, and every fork-race that resumes from a snapshot) depend on the
host's CPython *patch* version rather than the ``>=3.12`` the project claims to
support - a contributor on a stale interpreter would see spurious failures.

The autouse fixture below pins the patch check to ``True`` so these tests
exercise extraction *logic* independent of host patch level. The dedicated
negative test (``test_extract_refuses_on_unpatched_cpython``) re-patches it to
``False`` in its own body, which wins for the duration of that test.
"""

from __future__ import annotations

import pytest

from bernstein.core.sandbox.backends import _vmmonitor


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "raw_tarfile_filter: do not auto-patch _tarfile_data_filter_is_patched "
        "(for tests exercising the real version-matrix logic)",
    )


@pytest.fixture(autouse=True)
def _assume_patched_tarfile_filter(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    # Tests that exercise the real version matrix opt out via the marker;
    # everything else gets the check pinned to True so round-trip / correctness
    # tests are independent of the host's CPython patch level.
    if request.node.get_closest_marker("raw_tarfile_filter"):
        return
    monkeypatch.setattr(_vmmonitor, "_tarfile_data_filter_is_patched", lambda: True)
