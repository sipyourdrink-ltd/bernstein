"""Lightweight import-presence check used by the auto-heal v2 workflow.

The workflow needs to assert at runtime that certain integration
modules are reachable (they form the contract the v2 layers depend on)
without instantiating real state. This helper takes one of a small set
of module aliases and exits 0 if the import works, non-zero otherwise.

The supported aliases are intentionally narrow so the YAML cannot
import arbitrary modules.
"""

from __future__ import annotations

import importlib
import sys

_ALIASES: dict[str, str] = {
    "blast_radius": "bernstein.core.quality.blast_radius",
    "decision_log": "bernstein.core.observability.decision_log",
    "calibration": "bernstein.eval.calibration",
    "permission_policy": "bernstein.core.security.permission_policy",
    "lineage_v2": "bernstein.core.lineage.v2_store",
    "lineage_writer": "bernstein.core.autoheal.lineage_writer",
    "cordon": "bernstein.core.autoheal.cordon",
    "bandit": "bernstein.core.autoheal.bandit",
    "bayesian": "bernstein.core.autoheal.bayesian",
    "shadow_mode": "bernstein.core.autoheal.shadow_mode",
    "kill_switch": "bernstein.core.autoheal.kill_switch",
    "audit_log": "bernstein.core.autoheal.audit_log",
    "idempotency": "bernstein.core.autoheal.idempotency",
    "wire": "bernstein.core.autoheal.wire",
}


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        sys.stderr.write("usage: auto_heal_v2_imports.py <alias>\n")
        return 2
    alias = args[0]
    target = _ALIASES.get(alias)
    if target is None:
        sys.stderr.write(f"unknown alias: {alias}\n")
        return 2
    try:
        importlib.import_module(target)
    except ImportError as e:
        sys.stderr.write(f"import failed for {target}: {e}\n")
        return 1
    sys.stdout.write(f"import OK: {target}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
