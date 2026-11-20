"""Print the quality gate settings parsed from a Bernstein seed."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from bernstein.core.config.seed_config import SeedError
from bernstein.core.config.seed_parser import parse_seed


def inspect_seed(path: Path) -> dict[str, object]:
    gates = parse_seed(path).quality_gates
    if gates is None:
        raise SeedError(f"{path} has no quality_gates section")
    values = gates.to_dict()
    return {
        key: values[key]
        for key in (
            "mutation_testing",
            "mutation_threshold",
            "large_file_check",
            "large_file_threshold",
            "test_expansion",
            "intent_verification",
        )
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("seed", type=Path, nargs="?", default=Path(__file__).with_name("bernstein.yaml"))
    args = parser.parse_args()
    try:
        values = inspect_seed(args.seed)
    except SeedError as error:
        parser.exit(2, f"{error}\n")
    print(json.dumps(values, indent=2))


if __name__ == "__main__":
    main()
