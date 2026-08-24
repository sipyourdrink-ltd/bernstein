#!/usr/bin/env python3
"""Deterministic probe fixture: fixed ``--version`` and ``--help`` output.

Output is byte-stable across runs so the content-addressed evidence hash is
stable. Any other invocation (e.g. shell-completion patterns) prints a fixed
fallback line and exits 0.
"""

import sys


def main() -> int:
    if "--version" in sys.argv:
        print("probe-fixture 1.2.3")
        return 0
    if "--help" in sys.argv:
        print("probe-fixture: deterministic help text")
        return 0
    print("probe-fixture: unknown invocation")
    return 0


if __name__ == "__main__":
    sys.exit(main())
