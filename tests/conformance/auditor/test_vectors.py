"""The 21 auditor questions, as vectors over the recorded bundle.

One vector is implemented: question 17. The remaining twenty are named
in :mod:`tests.conformance.auditor.questions` and land in their own
slices; until then the scoreboard reports them as unanswered, which is
the honest reading of the evidence rather than a weak assertion that
passes.

Every vector answers its question from the exported bundle alone. The
bundle reader refuses any path outside the export, so a vector cannot
reach the ``.sdd/`` that produced it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.conformance.auditor import offline, recorder

if TYPE_CHECKING:
    from pathlib import Path

    from tests.conformance.auditor.bundle import BundleReader


@pytest.mark.question(17)
def test_q17_bundle_verifies_with_no_network_and_no_bernstein_install(
    bundle_reader: BundleReader,
    trust_anchor: Path,
    auditor_env: offline.AuditorEnvironment,
) -> None:
    """Q17: can the bundle be verified with no network and no install?

    The receipt is verified by ``verify_cli/`` in a subprocess whose
    import path carries the standalone verifier and its two dependencies
    and nothing else - ``bernstein`` is not importable there, which
    :func:`test_verifier_subprocess_cannot_import_bernstein` proves - and
    whose audit hook denies every socket call. The operator's public key
    is pinned from outside the bundle, so a bundle that re-signed itself
    cannot answer this question with its own key.
    """
    receipt = bundle_reader.resolve(recorder.AUDIT_RECEIPT_NAME)
    result = offline.verify_receipt(auditor_env, receipt=receipt, trust_anchor=trust_anchor)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "OVERALL: PASS" in result.stdout
    for check in ("cose", "intoto", "transparency", "subject_binding"):
        assert f"[PASS] {check}" in result.stdout
