"""Security posture scoring.

:func:`compute_evidenced_posture` (issue #4989) answers the question an
operator asks -- "how is this install doing" -- and answers it *only* from
facts the chain evidences. It consumes the per-control coverage report
(:func:`bernstein.core.compliance.coverage.assess_control_coverage`) and derives
nothing itself, so the number moves when the install produces evidence and does
not move when someone switches a control on. A control that is enabled but
produced no chain event contributes nothing.

The document names every input: the control, its weight, whether the chain
evidenced it, and the content hash of each chain event that did. It carries the
version of the weight table that produced it, because a score without its
weights cannot be recomputed. It carries its own denominator -- the weight that
was *measurable*, not the weight that exists -- so a high score over a small
surface reads as a high score over a small surface.

Determinism: the document is canonical JSON (sorted keys, minimal separators)
and every field is a pure function of the chain, so a third party recomputing
from the same entries gets byte-identical bytes and the same HMAC.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.compliance.coverage import (
    ControlCoverageResult,
    ControlCoverageStatus,
    assess_control_coverage,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Evidenced posture (issue #4989): a score derived only from chain evidence
# ---------------------------------------------------------------------------

#: Shape version of the evidenced-posture document. Bump only on a shape change.
EVIDENCED_POSTURE_VERSION = 1

#: Version of the weight table below. It travels in every document: a score
#: read without the weights that produced it cannot be recomputed, and two
#: installs on different weight tables are not comparable numbers.
POSTURE_WEIGHTS_VERSION = "evidenced-posture-weights/1"

#: Per-control weights. Data, not code -- a control is a row, and re-weighting
#: is an edit here plus a bump of POSTURE_WEIGHTS_VERSION.
CONTROL_WEIGHTS: dict[str, float] = {
    "eu-ai-act-art-12": 1.0,
    "eu-ai-act-art-14": 1.0,
    "eu-ai-act-art-73": 1.0,
    "soc2-cc6-1": 1.0,
    "soc2-cc8-1": 1.0,
}

#: Weight for a measurable control the table does not name. Uniform rather than
#: zero: a control the chain can evidence but the table forgot must still show
#: up in the denominator, or adding a control would silently raise the score.
DEFAULT_CONTROL_WEIGHT = 1.0

#: Decimal places the score is rounded to, fixed so two surfaces computing the
#: same fraction serialise the same digits.
_SCORE_PRECISION = 6


@dataclass(frozen=True, slots=True)
class PostureContribution:
    """One control's contribution to the score, and the events behind it."""

    policy_id: str
    control_id: str
    weight: float
    evidenced: bool
    chain_events: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "control_id": self.control_id,
            "weight": self.weight,
            "evidenced": self.evidenced,
            "chain_events": list(self.chain_events),
        }


@dataclass(frozen=True, slots=True)
class EvidencedPosture:
    """A posture score and every input that produced it.

    Attributes:
        score: ``evidenced_weight / measurable_weight`` as a percentage, or
            ``None`` when nothing was measurable. Zero over zero is absent
            evidence; rendering it as ``0`` or ``100`` is how a console comes
            to claim what it cannot show.
        weights_version: The weight table that produced *score*.
        evidenced_weight: Numerator -- weight of the controls the chain evidences.
        measurable_weight: Denominator -- weight of the controls that were
            measurable at all.
        measurable_controls: Count behind *measurable_weight*.
        registered_controls: Controls the coverage report considered, measurable
            or not. Read against *measurable_controls* it says how much of the
            surface the score is silent about.
        contributions: One row per measurable control, sorted by control id.
    """

    score: float | None
    weights_version: str
    evidenced_weight: float
    measurable_weight: float
    measurable_controls: int
    registered_controls: int
    contributions: tuple[PostureContribution, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return the unsigned canonical payload."""
        return {
            "v": EVIDENCED_POSTURE_VERSION,
            "score": self.score,
            "weights_version": self.weights_version,
            "evidenced_weight": self.evidenced_weight,
            "measurable_weight": self.measurable_weight,
            "measurable_controls": self.measurable_controls,
            "registered_controls": self.registered_controls,
            "contributions": [c.to_dict() for c in self.contributions],
        }


def compute_evidenced_posture(coverage: Sequence[ControlCoverageResult]) -> EvidencedPosture:
    """Score *coverage* using only the controls the chain could speak to.

    A control the install cannot evidence at all
    (:attr:`~bernstein.core.compliance.coverage.ControlCoverageStatus.NOT_EVIDENCEABLE`)
    is held out of both numerator and denominator: scoring it would be scoring
    the absence of a measurement. Every other control is in the denominator,
    and only a control with chain evidence is in the numerator.

    Args:
        coverage: Per-control coverage results, as returned by
            :func:`~bernstein.core.compliance.coverage.assess_control_coverage`.

    Returns:
        The :class:`EvidencedPosture` for that report.
    """
    contributions: list[PostureContribution] = []
    evidenced_weight = 0.0
    measurable_weight = 0.0

    for result in sorted(coverage, key=lambda r: (r.control_id, r.policy_id)):
        if result.status is ControlCoverageStatus.NOT_EVIDENCEABLE:
            continue
        weight = CONTROL_WEIGHTS.get(result.policy_id, DEFAULT_CONTROL_WEIGHT)
        evidenced = result.status is ControlCoverageStatus.EVIDENCED
        measurable_weight += weight
        if evidenced:
            evidenced_weight += weight
        contributions.append(
            PostureContribution(
                policy_id=result.policy_id,
                control_id=result.control_id,
                weight=weight,
                evidenced=evidenced,
                chain_events=result.evidence_refs if evidenced else (),
            )
        )

    score = None if measurable_weight == 0 else round(evidenced_weight / measurable_weight * 100.0, _SCORE_PRECISION)

    return EvidencedPosture(
        score=score,
        weights_version=POSTURE_WEIGHTS_VERSION,
        evidenced_weight=evidenced_weight,
        measurable_weight=measurable_weight,
        measurable_controls=len(contributions),
        registered_controls=len(coverage),
        contributions=tuple(contributions),
    )


def collect_evidenced_posture(workdir: Path) -> EvidencedPosture:
    """Project the evidenced posture of the install rooted at *workdir*.

    Reads ``<workdir>/.sdd/lineage`` and nothing else. No configuration file is
    consulted, so enabling a control cannot move the number.
    """
    from bernstein.core.lineage.store import LineageStore

    store = LineageStore(workdir / ".sdd" / "lineage")
    entries = [entry for entry, _ in store.read_log()]
    return compute_evidenced_posture(assess_control_coverage(entries))


def evidenced_posture_json(workdir: Path, *, hmac_key: bytes) -> str:
    """Return the signed, canonical evidenced-posture document for *workdir*.

    **The** entry point: the operator command prints exactly these bytes, so
    the screen and an offline recomputation from ``.sdd`` cannot differ. The
    signature is HMAC-SHA256 over the canonical bytes of the document without
    its ``signature`` field, keyed with the audit-chain key.

    Args:
        workdir: Project root containing ``.sdd``.
        hmac_key: Audit-chain HMAC key to sign the document with.
    """
    payload = collect_evidenced_posture(workdir).to_dict()
    payload["signature"] = _hmac.new(hmac_key, _canonical_bytes(payload), hashlib.sha256).hexdigest()
    return _canonical_bytes(payload).decode("utf-8")


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    """Return canonical JSON bytes (sorted keys, minimal separators, UTF-8)."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def format_evidenced_posture(posture: EvidencedPosture) -> str:
    """Render an evidenced posture as a Rich-formatted string."""
    from io import StringIO

    from rich.console import Console
    from rich.table import Table
    from rich.text import Text

    buf = StringIO()
    console = Console(file=buf, force_terminal=True, width=100)

    headline = "not measurable" if posture.score is None else f"{posture.score:.1f}/100"
    console.print(
        Text.assemble(
            ("Evidenced posture: ", "bold"),
            (headline, "bold"),
        )
    )
    console.print(
        f"{posture.evidenced_weight:g}/{posture.measurable_weight:g} weight over "
        f"{posture.measurable_controls} of {posture.registered_controls} controls  |  "
        f"weights {posture.weights_version}\n"
    )

    table = Table(title="Contributions", expand=True)
    table.add_column("Control", style="cyan")
    table.add_column("Weight", justify="right")
    table.add_column("Evidenced", justify="center")
    table.add_column("Chain events")
    for c in posture.contributions:
        table.add_row(
            c.control_id,
            f"{c.weight:g}",
            "[green]yes[/green]" if c.evidenced else "[red]no[/red]",
            "\n".join(c.chain_events) or "-",
        )
    console.print(table)
    return buf.getvalue()
