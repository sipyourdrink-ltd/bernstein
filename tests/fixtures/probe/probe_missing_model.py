#!/usr/bin/env python3
"""Deterministic probe fixture: help output deliberately omits --model.

This fixture simulates a CLI whose --help output is incomplete from the
operator's perspective: it does not expose a ``--model`` flag at all.
The probe evidence generated from this fixture is used to assert that
the drafting step refuses and names the missing field rather than
silently accepting a profile with a guessed default.
"""

import sys


def main() -> int:
    if "--version" in sys.argv:
        print("probe-missing-model 1.0.0")
        return 0
    if "--help" in sys.argv:
        print(
            "probe-missing-model: a deterministic probe fixture\n"
            "\n"
            "Usage: probe-missing-model [OPTIONS] [ARGS]...\n"
            "\n"
            "Options:\n"
            "  --prompt <text>   Supply the prompt inline\n"
            "  --subcommand <cmd>\n"
            "                    Choose a subcommand\n"
            "  --env <key=val>   Pass environment variables\n"
            "  --help            Show this message\n"
        )
        return 0
    print("probe-missing-model: unknown invocation")
    return 0


if __name__ == "__main__":
    sys.exit(main())
