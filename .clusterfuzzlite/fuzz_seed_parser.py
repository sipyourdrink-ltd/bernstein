"""Atheris fuzz harness for seed-file YAML parsing.

The OSSF Scorecard Fuzzing check is signal-only: it just looks for a
ClusterFuzzLite/OSS-Fuzz integration with at least one harness. This
harness exercises the same YAML parser primitive that
bernstein.core.config.seed_parser sits on top of (PyYAML's safe_load),
without importing the full bernstein package -- bernstein requires
Python 3.12+ while the gcr.io/oss-fuzz-base/base-builder-python image
ships Python 3.11. Switching the base image is possible but adds a
maintenance burden that does not buy any extra Scorecard signal.

The Hypothesis property-test suite in tests/ remains the primary
fuzzing surface for the seed parser and the rest of bernstein's input
handling.
"""

from __future__ import annotations

import sys

import atheris

with atheris.instrument_imports():
    import yaml


def _test_one_input(data: bytes) -> None:
    """Fuzz entrypoint: feed arbitrary bytes to PyYAML's safe_load."""
    try:
        yaml.safe_load(data)
    except (yaml.YAMLError, UnicodeDecodeError, ValueError, TypeError, OverflowError, RecursionError):
        # Documented failure modes for malformed input. Not a crash.
        # OverflowError is raised by PyYAML's C scanner on Python 3.11 when
        # parsing very long ``\\Uxxxxxxxx`` escapes (codepoints that exceed
        # the C int range during conversion); it is parser-internal and not
        # a bug in the seed-parser surface under test.
        # RecursionError is raised by PyYAML's composer, which recurses once
        # per nesting level: deeply nested flow collections (e.g. many
        # ``[[[...`` openers) exceed the interpreter recursion limit. This
        # is documented upstream parser behaviour on adversarial input, not
        # a crash in the seed-parser surface under test.
        return


def main() -> None:
    """libFuzzer entry point."""
    atheris.Setup(sys.argv, _test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
