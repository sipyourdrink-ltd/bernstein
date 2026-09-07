"""Bundle comparison with harness-fingerprint drift enforcement."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from bernstein.eval.bench.fingerprint import find_differing_settings

if TYPE_CHECKING:
    from bernstein.eval.bench.bundle import SubmissionBundle


class HarnessDriftError(ValueError):
    """Raised when comparing bundles with differing harness fingerprints."""


@dataclass(frozen=True)
class CompareResult:
    score_delta: float
    pass_rate_delta: float
    fingerprint_match: bool
    differing_keys: list[str]
    allowed_drift: bool = False

    def report(self) -> str:
        lines: list[str] = []
        if not self.fingerprint_match:
            lines.append("Harness drift detected:")
            for k in self.differing_keys:
                lines.append(f"  - {k} differs between bundles")
            if not self.allowed_drift:
                lines.append(
                    "Refusing to rank bundles with differing harnesses (pass --allow-harness-drift to override)."
                )
                return "\n".join(lines)
            lines.append("Ranking permitted via --allow-harness-drift.")
        lines.append(f"Score delta     : {self.score_delta:+.4f}")
        lines.append(f"Pass rate delta : {self.pass_rate_delta:+.2%}")
        return "\n".join(lines)


def compare_bundles(
    bundle_a: SubmissionBundle,
    bundle_b: SubmissionBundle,
    *,
    allow_harness_drift: bool = False,
) -> CompareResult:
    """Compare two submission bundles, refusing to rank if harness fingerprints differ."""
    fp_a = bundle_a.harness_fingerprint
    fp_b = bundle_b.harness_fingerprint
    match = fp_a == fp_b

    differing = find_differing_settings(bundle_a.harness_settings, bundle_b.harness_settings)
    if not match and not allow_harness_drift:
        raise HarnessDriftError(
            f"Cannot compare bundles with different harness fingerprints ({fp_a[:8]} vs {fp_b[:8]}). "
            f"Differing settings: {', '.join(differing) if differing else 'fingerprint mismatch'}. "
            "Use --allow-harness-drift to compare anyway."
        )

    score_delta = bundle_b.overall_score - bundle_a.overall_score
    pass_rate_delta = bundle_b.pass_rate - bundle_a.pass_rate

    return CompareResult(
        score_delta=score_delta,
        pass_rate_delta=pass_rate_delta,
        fingerprint_match=match,
        differing_keys=differing,
        allowed_drift=allow_harness_drift,
    )
