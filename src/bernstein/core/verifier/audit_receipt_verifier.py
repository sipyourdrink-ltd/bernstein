"""Console entry point for verifying Bernstein audit receipts standalone.

This module wraps the hermetic verifier at ``tools/verify_audit_receipt.py``
so it can be installed as a console script. It has zero dependence on the
Bernstein orchestrator or any internal keys - only ``cryptography`` and
``cbor2`` are required, matching what an external auditor would run.

Usage::

    verify-audit-receipt --receipt /path/to/receipt.json [options]

See ``tools/verify_audit_receipt.py --help`` for full option details.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_CURRENT_FILE = Path(__file__).resolve()

# Source checkout: this file is src/bernstein/core/verifier/, the script sits
# under tools/ at the repo root, four levels up.
_DEV_VERIFIER_SCRIPT = _CURRENT_FILE.parents[4] / "tools" / "verify_audit_receipt.py"

# Installed wheel: tools/ is not part of the package, so the script is
# force-included next to the other bundled defaults
# ([tool.hatch.build.targets.wheel.force-include] in pyproject.toml).
_PACKAGED_VERIFIER_SCRIPT = _CURRENT_FILE.parents[2] / "_default_templates" / "tools" / "verify_audit_receipt.py"


def _resolve_verifier_script() -> Path:
    """Return the checkout copy when present, else the wheel-bundled copy."""
    return _DEV_VERIFIER_SCRIPT if _DEV_VERIFIER_SCRIPT.is_file() else _PACKAGED_VERIFIER_SCRIPT


VERIFIER_SCRIPT = _resolve_verifier_script()


def main(argv: list[str] | None = None) -> int:
    """Entry point for the verify-audit-receipt console script.

    Delegates directly to the hermetic verifier's main() function via subprocess
    to guarantee identical behavior to the standalone script. This avoids import
    conflicts and ensures the verifier runs with only its declared dependencies
    (cryptography, cbor2 - no bernstein imports).

    Returns the same exit codes: 0=pass, 1=fail, 2=bad args.
    """
    if argv is None:
        argv = sys.argv[1:]

    result = subprocess.run([sys.executable, str(VERIFIER_SCRIPT), *argv], check=False)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
