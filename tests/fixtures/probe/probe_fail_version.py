#!/usr/bin/env python3
"""Deterministic probe fixture: exits non-zero on ``--version``.

Used to exercise the failed-probe evidence path: the exit code and captured
stderr are recorded in the evidence file rather than raising.
"""

import sys


def main() -> int:
    if "--version" in sys.argv:
        print("probe-fixture: version check failed", file=sys.stderr)
        return 2
    if "--help" in sys.argv:
        print("probe-fixture: deterministic help text")
        return 0
    print("probe-fixture: unknown invocation")
    return 0


if __name__ == "__main__":
    sys.exit(main())
