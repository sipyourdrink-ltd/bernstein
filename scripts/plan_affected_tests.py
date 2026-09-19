#!/usr/bin/env python3
"""Plan affected tests once, then select deterministic shards from that plan."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import TypedDict, cast

from run_tests import (
    changed_files_require_tests,
    default_shard_durations_path,
    discover_affected_files,
    discover_changed_files,
    discover_whole_tree_guard_files,
    load_shard_durations,
    parse_shard_spec,
    shard_files,
)

ROOT = Path(__file__).resolve().parent.parent
PLAN_VERSION = 1


class AffectedTestPlan(TypedDict):
    version: int
    base: str
    base_sha: str
    head_sha: str
    affected: list[str]
    guards: list[str]


def _relative(path: Path) -> str:
    """Return a portable repository-relative test path."""
    absolute = path if path.is_absolute() else ROOT / path
    return absolute.resolve().relative_to(ROOT.resolve()).as_posix()


def _resolve(rev: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", rev],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def build_plan(base: str) -> AffectedTestPlan:
    """Resolve the affected set and its fixed comparison commits."""
    affected = discover_affected_files(base)
    if not affected:
        changed = discover_changed_files(base)
        deleted = discover_changed_files(base, diff_filter="D")
        if changed_files_require_tests(changed, deleted):
            raise RuntimeError(
                f"No affected tests found for code or workflow changes; compared {_resolve(base)}...{_resolve('HEAD')}."
            )
    return {
        "version": PLAN_VERSION,
        "base": base,
        "base_sha": _resolve(base),
        "head_sha": _resolve("HEAD"),
        "affected": [_relative(path) for path in affected],
        "guards": [_relative(path) for path in discover_whole_tree_guard_files()],
    }


def read_plan(path: Path) -> AffectedTestPlan:
    """Read a plan produced by :func:`build_plan`."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    scalar_fields = ("base", "base_sha", "head_sha")
    list_fields = ("affected", "guards")
    if (
        not isinstance(raw, dict)
        or raw.get("version") != PLAN_VERSION
        or any(not isinstance(raw.get(field), str) for field in scalar_fields)
        or any(
            not isinstance(raw.get(field), list) or not all(isinstance(item, str) for item in raw[field])
            for field in list_fields
        )
    ):
        raise ValueError(f"unsupported affected-test plan: {path}")
    return cast("AffectedTestPlan", raw)


def select_shard(plan: AffectedTestPlan, spec: str, durations_path: Path) -> list[str]:
    """Return one shard, adding whole-tree guards to shard one only."""
    index, count = parse_shard_spec(spec)
    affected = [Path(path) for path in plan["affected"]]
    durations = load_shard_durations(durations_path)
    selected = shard_files(affected, index, count, durations=durations or None)
    if index == 1:
        selected = sorted(set(selected) | {Path(path) for path in plan["guards"]})
    return [path.as_posix() for path in selected]


def _write_selection(plan: AffectedTestPlan, spec: str, output: Path, durations_path: Path) -> None:
    selected = select_shard(plan, spec, durations_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("".join(f"{path}\n" for path in selected), encoding="utf-8")
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with Path(github_output).open("a", encoding="utf-8") as stream:
            stream.write(f"selected_count={len(selected)}\n")
    if selected:
        return
    message = f"No affected test files in shard {spec}. Compared {plan['base_sha']}...{plan['head_sha']}."
    print(message)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::notice title=Nothing to run::{message}")


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)

    plan_parser = commands.add_parser("plan")
    plan_parser.add_argument("--base", required=True)
    plan_parser.add_argument("--output", type=Path, required=True)

    select_parser = commands.add_parser("select")
    select_parser.add_argument("--plan", type=Path, required=True)
    select_parser.add_argument("--shard", required=True)
    select_parser.add_argument("--output", type=Path, required=True)
    select_parser.add_argument(
        "--durations-file",
        type=Path,
        default=default_shard_durations_path(),
    )

    args = parser.parse_args()
    if args.command == "plan":
        plan = build_plan(args.base)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(plan, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    else:
        _write_selection(read_plan(args.plan), args.shard, args.output, args.durations_file)


if __name__ == "__main__":
    main()
