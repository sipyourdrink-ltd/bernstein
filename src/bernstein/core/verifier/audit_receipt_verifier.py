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

# Resolve the path to the standalone verifier script.
# This file is in src/bernstein/core/verifier/, so we go up 4 levels to the project root.
_CURRENT_FILE = Path(__file__).resolve()
_PROJECT_ROOT = _CURRENT_FILE.parents[4]
_VERIFIER_SCRIPT = _PROJECT_ROOT / "tools" / "verify_audit_receipt.py"


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

    result = subprocess.run([sys.executable, str(_VERIFIER_SCRIPT), *argv], check=False)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
