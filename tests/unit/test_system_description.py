"""The system describes itself the same way everywhere (#5004).

Three code paths described a narrower system than the one that ships: the EU AI
Act evidence package, the ``/.well-known/`` agent payload, and the Cursor rules
that land in a user's editor. All three said "orchestrator for CLI coding
agents" while ``README.md``, ``AGENTS.md``, ``pyproject.toml``, ``action.yml``
and ``agents.txt`` already described the governance layer.

One of the three lands verbatim in the artifact handed to an assessor, so the
package understated the scope of the system it assesses.

The drift happened because these strings live in CODE rather than in docs, so a
docs pass does not reach them. This is the part that keeps it fixed: without it
the same three files drift again on the next change.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.compliance.eu_ai_act import bernstein_descriptor
from bernstein.core.routes import well_known
from bernstein.system_description import (
    DEPLOYMENT_CONTEXT,
    INTENDED_USE,
    SYSTEM_DESCRIPTION,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Words that name the system by what it governs. Asserted as a property of the wording
#: rather than as an exact sentence: pinning the prose would redden on every edit and get
#: the test deleted, while these are the terms whose ABSENCE was the defect.
GOVERNANCE_TERMS = ("governance", "governs", "governing")


def test_the_evidence_package_and_well_known_read_from_one_string() -> None:
    """Equality, not similarity — the two cannot drift while they are one constant."""
    assert bernstein_descriptor().description == SYSTEM_DESCRIPTION
    assert well_known._AGENT_DESCRIPTION == SYSTEM_DESCRIPTION  # pyright: ignore[reportPrivateUsage]


def test_the_assessor_facing_description_is_not_narrower_than_the_system() -> None:
    """The defect, stated as a property: it described the default workload as the whole subject."""
    descriptor = bernstein_descriptor()
    assert any(term in descriptor.description.lower() for term in GOVERNANCE_TERMS)
    assert any(term in descriptor.intended_use.lower() for term in GOVERNANCE_TERMS)
    assert descriptor.deployment_context == DEPLOYMENT_CONTEXT


def test_intended_use_covers_more_than_software_development() -> None:
    """Criterion 3. The recording and gating path never inspects what an agent produced.

    A diff, a report, a dataset and an evidence pack travel it identically, so an
    intended-use naming code alone is narrower than the code it describes.
    """
    assert INTENDED_USE is bernstein_descriptor().intended_use
    lowered = INTENDED_USE.lower()
    assert "research" in lowered or "dataset" in lowered or "evidence" in lowered


def test_cli_coding_agents_are_still_named() -> None:
    """Criterion 2, and the over-correction guard.

    They are the out-of-the-box path. Dropping them swaps one inaccuracy for another,
    and the claim being fixed is that they were the ONLY subject, not that they are a
    subject.
    """
    assert "cli coding agents" in SYSTEM_DESCRIPTION.lower()


@pytest.mark.parametrize(
    "path",
    [
        Path("src/bernstein/compliance/eu_ai_act.py"),
        Path("src/bernstein/core/routes/well_known.py"),
        Path("packages/cursor-plugin/rules/bernstein-context.mdc"),
    ],
)
def test_no_site_calls_the_product_an_orchestrator_for_cli_agents(path: Path) -> None:
    """The exact phrasing that drifted, pinned at each of the three sites.

    Narrow on purpose: "the orchestrator forwards / reaps / kills" names the COMPONENT,
    which is still an orchestrator, and `adapters/base.py` really is the CLI-coding-agent
    adapter base. Only descriptions of the PRODUCT are in scope.

    Python COMMENTS are excluded: a comment recording what a string used to say is the
    same kind of artifact as an ADR - it documents the change rather than making the
    claim - and the note in `well_known.py` quoting the old wording would otherwise fail
    this. Not excluded in the `.mdc`, where `#` opens a heading rather than a comment, and
    the heading is one of the places the old wording lived.
    """
    lines = (REPO_ROOT / path).read_text(encoding="utf-8").lower().splitlines()
    if path.suffix == ".py":
        lines = [ln for ln in lines if not ln.strip().startswith("#")]
    text = "\n".join(lines)
    for phrase in ("orchestrator for cli coding", "orchestrator for short-lived cli"):
        assert phrase not in text, f"{path} still describes the product as {phrase!r}"


def test_the_wording_stays_consistent_with_agents_md() -> None:
    """AGENTS.md is the curated description the rest of the project was updated to.

    Checked on the claim rather than by string equality: AGENTS.md is generated prose and
    this is a machine-readable field, so they are the same statement in two registers.
    """
    overview = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8").lower()
    assert "governance layer" in overview
    assert "governance layer" in SYSTEM_DESCRIPTION.lower()
