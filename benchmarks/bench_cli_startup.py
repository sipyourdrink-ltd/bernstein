"""Measure fresh-process CLI exit latency with isolated application and bytecode caches."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import NotRequired, TypedDict

_COMMANDS = {
    "help": ["--help"],
    "version": ["--version"],
    "config-path": ["config-path", "--json"],
    "run-help": ["run", "--help"],
}


class Sample(TypedDict):
    seconds: float
    exit: int
    stdout_sha256: str
    stderr_sha256: str


class Summary(TypedDict):
    median: float
    samples: list[Sample]
    p95: NotRequired[float]


def _run(
    entry: list[str],
    args: list[str],
    cache: Path | None,
    timeout: float,
) -> tuple[Sample, bytes, bytes]:
    with tempfile.TemporaryDirectory(prefix="bernstein-startup-") as location:
        fixture = Path(location)
        paths = {
            "HOME": "home",
            "XDG_CONFIG_HOME": "config",
            "XDG_CACHE_HOME": "cache",
            "XDG_DATA_HOME": "data",
        }
        for name in [*paths.values(), "project"]:
            (fixture / name).mkdir()
        env = {
            "PATH": os.environ.get("PATH", os.defpath),
            "LANG": "en_US.UTF-8",
            "TERM": "dumb",
            "COLUMNS": "80",
            "USERPROFILE": str(fixture / "home"),
            "PYTHONPYCACHEPREFIX": str(cache if cache is not None else fixture / "bytecode"),
            **{name: str(fixture / directory) for name, directory in paths.items()},
        }
        if "SYSTEMROOT" in os.environ:
            env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
        start = time.perf_counter()
        result = subprocess.run(
            [*entry, *args],
            cwd=fixture / "project",
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        elapsed = time.perf_counter() - start
        stdout = result.stdout.replace(os.fsencode(fixture), b"/FIXTURE")
        stderr = result.stderr.replace(os.fsencode(fixture), b"/FIXTURE")
        sample: Sample = {
            "seconds": elapsed,
            "exit": result.returncode,
            "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
            "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
        }
        return sample, stdout, stderr


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--console", type=Path, help="Path to the installed bernstein script.")
    parser.add_argument("--entrypoint", choices=["console", "module"], default="console")
    parser.add_argument("--python", type=Path, default=Path(sys.executable), help="Python for module runs and traces.")
    parser.add_argument("--command", choices=_COMMANDS, action="append", help="Defaults to help and config-path.")
    parser.add_argument("--runs", type=int, default=5, help="Number of samples per command and cache mode.")
    parser.add_argument("--cache", choices=["both", "cold", "warm"], default="both")
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--importtime-dir", type=Path, help="Write separate cold import traces after the timed runs.")
    options = parser.parse_args()
    if options.runs < 1:
        parser.error("--runs must be positive")
    if options.timeout <= 0:
        parser.error("--timeout must be positive")
    python = str(options.python.absolute())
    if options.entrypoint == "module":
        entry = [python, "-m", "bernstein"]
    else:
        launcher = options.console or shutil.which("bernstein")
        if launcher is None:
            parser.error("bernstein was not found on PATH. Supply --console.")
        entry = [str(Path(launcher).absolute())]

    commands = options.command or ["help", "config-path"]
    modes = ("cold", "warm") if options.cache == "both" else (options.cache,)
    results: dict[str, Summary] = {}
    with tempfile.TemporaryDirectory(prefix="bernstein-startup-warm-") as location:
        for mode in modes:
            cache = Path(location) if mode == "warm" else None
            for command in commands:
                args = _COMMANDS[command]
                if cache is not None:
                    primer, _, _ = _run(entry, args, cache, options.timeout)
                    if primer["exit"]:
                        print(f"{command} warm-up returned {primer['exit']}", file=sys.stderr)
                        return 1
                samples = [_run(entry, args, cache, options.timeout)[0] for _ in range(options.runs)]
                seconds = sorted(sample["seconds"] for sample in samples)
                summary: Summary = {"median": statistics.median(seconds), "samples": samples}
                if len(seconds) >= 50:
                    summary["p95"] = seconds[math.ceil(len(seconds) * 0.95) - 1]
                results[f"{command} {mode}"] = summary

    print(json.dumps({"platform": platform.platform(), "entrypoint": options.entrypoint, "results": results}, indent=2))
    if options.importtime_dir is not None:
        options.importtime_dir.mkdir(parents=True, exist_ok=True)
        for command in commands:
            sample, _, stderr = _run(
                [python, "-X", "importtime", "-m", "bernstein"], _COMMANDS[command], None, options.timeout
            )
            (options.importtime_dir / f"{command}.cold.importtime").write_bytes(stderr)
            if sample["exit"]:
                return 1
    return int(any(sample["exit"] for summary in results.values() for sample in summary["samples"]))


if __name__ == "__main__":
    raise SystemExit(main())
