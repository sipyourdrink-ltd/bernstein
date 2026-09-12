"""What a policy-library pass actually asserts, measured rather than described.

`docs/operations/compliance.md` used to list SOC 2, ISO 27001, PCI-DSS and
NIST 800-53 as "Shipped" in the same column as the EU AI Act, whose check walks
every HMAC link in a recorded chain. The checks behind the other four read
configuration, and most of them are satisfied by a key being present at all --
`check_auth_configured` is `"auth" in config`, so an empty `auth:` section
passes. Both claims under one "Shipped" set the same expectation, and only one
survives a reviewer asking what the check proves (#5056).

The docs now separate the two. These tests measure the property the docs
assert, so the page cannot drift from the code: if someone strengthens
`check_auth_configured` to resolve the section, the count here moves and the
paragraph naming it has to be rewritten.

This pins what the checks *do*, not what they should do. Option B of the issue
-- making the checks assert something about the resolved configuration -- is a
separate change, deliberately not made here.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml

from bernstein.core.security import compliance_library

if TYPE_CHECKING:
    from bernstein.core.security.compliance_library import PolicyResult

DOCS = Path(__file__).resolve().parents[2] / "docs" / "operations" / "compliance.md"

#: Config keys the library looks for, all present and all empty. A check that
#: passes against this is satisfied by presence alone.
_PRESENT_BUT_EMPTY = (
    "auth",
    "state_encryption",
    "rbac",
    "audit",
    "logging",
    "retention",
    "backup",
    "tls",
    "incident_response",
    "secrets",
    "vulnerability_scanning",
    "change_management",
    "network_isolation",
    "logging_integrity",
    "session",
    "password_policy",
    "mfa",
    "rate_limit",
    "rate_limiting",
    "dependencies",
    "privacy",
    "data_classification",
    "phi_detection",
    "consent",
    "compliance",
    "security",
)

#: The count the docs paragraph states. Pinned so the prose and the code move
#: together: a check that gains a real assertion lowers this number.
PRESENCE_SATISFIED_COUNT = 14


def _checks() -> dict[str, Callable[[Path], PolicyResult]]:
    return {
        name: value for name, value in vars(compliance_library).items() if name.startswith("check_") and callable(value)
    }


@pytest.fixture(scope="module")
def hollow_project() -> Path:
    """A project whose config declares every key and configures nothing."""
    root = Path(tempfile.mkdtemp())
    hollow = {key: {} for key in _PRESENT_BUT_EMPTY}
    (root / "bernstein.yaml").write_text(yaml.safe_dump(hollow), encoding="utf-8")
    (root / ".sdd").mkdir()
    (root / ".sdd" / "config.yaml").write_text(yaml.safe_dump(hollow), encoding="utf-8")
    return root


def _passing_on(project: Path) -> set[str]:
    passing: set[str] = set()
    for name, check in _checks().items():
        try:
            if check(project).passed:
                passing.add(name)
        except Exception:
            continue
    return passing


def test_the_library_has_the_checks_this_measures() -> None:
    """A vacuous pass here would make every count below meaningless."""
    assert len(_checks()) > 20


def test_most_checks_are_satisfied_by_key_presence_alone(hollow_project: Path) -> None:
    """The measurement the docs paragraph cites.

    A configuration that declares every key and configures none of them is the
    clearest case of declared posture there is, and it passes most of the
    library.
    """
    passing = _passing_on(hollow_project)
    assert len(passing) == PRESENCE_SATISFIED_COUNT, (
        f"{len(passing)} checks pass on a present-but-empty config, docs say "
        f"{PRESENCE_SATISFIED_COUNT}. Update the count in "
        "docs/operations/compliance.md and in compliance_library's module "
        f"docstring.\nPassing: {sorted(passing)}"
    )


def test_the_named_example_still_passes_on_an_empty_section(hollow_project: Path) -> None:
    """The docs name `check_auth_configured` specifically; keep that true."""
    assert compliance_library.check_auth_configured(hollow_project).passed is True


def test_a_project_that_declares_nothing_fails_the_library() -> None:
    """The failure the library is genuinely good at catching.

    A declared-posture lint is the right tool for "nobody configured it at
    all", and saying what it does not prove is not a reason to stop trusting
    what it does.
    """
    root = Path(tempfile.mkdtemp())
    (root / "bernstein.yaml").write_text("{}\n", encoding="utf-8")
    assert compliance_library.check_auth_configured(root).passed is False
    assert compliance_library.check_encryption_at_rest(root).passed is False


def test_the_docs_table_separates_the_two_classes() -> None:
    """The column exists and puts the policy-library frameworks on one side."""
    text = DOCS.read_text(encoding="utf-8")
    assert "What a pass asserts" in text
    for framework in ("SOC 2", "ISO 27001", "PCI-DSS", "NIST 800-53"):
        row = next(line for line in text.splitlines() if f"**{framework}**" in line)
        assert "Declared posture" in row, f"{framework} should be marked declared posture"
    eu_row = next(line for line in text.splitlines() if "**EU AI Act**" in line)
    assert "Verified from evidence" in eu_row


def test_the_docs_count_matches_the_measurement() -> None:
    """The prose cites a number; the number is measured above."""
    assert f"{PRESENCE_SATISFIED_COUNT} of the 23 policy-library checks" in DOCS.read_text(encoding="utf-8")
