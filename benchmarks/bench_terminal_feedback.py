"""Measure a preparation message or rendered plan on a real POSIX terminal."""

from __future__ import annotations

import argparse
import contextlib
import errno
import hashlib
import json
import math
import os
import platform
import re
import selectors
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import psutil
from bench_local_readiness import _stop

# A brand mark, cursor movement or animation alone cannot satisfy the budget.
# Older releases have no preparation line. Their rendered goal also counts.
_GOAL = "Measure terminal startup"
_PREVIEW = re.compile(rb"Preparing run preview|Measure terminal startup", re.I)
_RUN = re.compile(rb"Preparing local run|Bernstein --idle mode|Goal:.*Measure terminal startup", re.I)


def measure(entry: list[str], args: list[str], home: Path, *, run: bool) -> dict[str, float | int | str]:
    import pty

    with tempfile.TemporaryDirectory(prefix="bernstein-preview-") as directory:
        env = {
            "PATH": os.defpath,
            "TERM": "xterm-256color",
            "HOME": str(home),
            "XDG_CACHE_HOME": str(home / "cache"),
            "XDG_DATA_HOME": str(home / "data"),
            "XDG_CONFIG_HOME": str(home / "config"),
            "LANG": "en_US.UTF-8",
        }
        project = Path(directory).resolve()
        cmd = [*entry, *args]
        if run:
            for git_args in (
                ["init", "-b", "benchmark"],
                [
                    "-c",
                    "user.name=Benchmark",
                    "-c",
                    "user.email=bench@example.invalid",
                    "commit",
                    "--allow-empty",
                    "-m",
                    "Fixture",
                ],
            ):
                subprocess.run(["git", *git_args], cwd=project, env=env, check=True, capture_output=True)
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            env.update(
                BERNSTEIN_SERVER_URL=f"http://127.0.0.1:{port}",
                BERNSTEIN_AUTH_TOKEN=uuid.uuid4().hex,
                GIT_ALLOW_PROTOCOL="file",
                GIT_TERMINAL_PROMPT="0",
                GIT_CONFIG_NOSYSTEM="1",
            )
            cmd.extend(["--port", str(port)])
        master, slave = pty.openpty()
        data = bytearray()
        first = None
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(master, selectors.EVENT_READ)
                start = time.perf_counter()
                with subprocess.Popen(cmd, cwd=project, env=env, stdin=slave, stdout=slave, stderr=slave) as proc:
                    owner = psutil.Process(proc.pid)
                    os.close(slave)
                    slave = -1
                    try:
                        while time.perf_counter() - start < 45:
                            if not selector.select(0.2):
                                continue
                            try:
                                block = os.read(master, 65536)
                            except OSError as exc:
                                if exc.errno != errno.EIO:
                                    raise
                                break
                            if not block:
                                break
                            data.extend(block)
                            if first is None and (_RUN if run else _PREVIEW).search(data):
                                first = time.perf_counter() - start
                                if run:
                                    # The ordinary invocation reaches its real first frame.
                                    # Request cancellation here; readiness is measured by
                                    # bench_local_readiness through the real dashboard.
                                    proc.send_signal(signal.SIGINT)
                        code = proc.wait(timeout=2)
                    finally:
                        if run:
                            _stop(project, owner)
                        proc.kill()
            # Preserve the actual interruption exit status, including older
            # releases that classified Ctrl-C as an internal error.
            if first is None or (not run and code != 0):
                raise RuntimeError(f"Command returned {code} with first frame {first}: {data[-700:]!r}")
            return {
                "first": first,
                "cursor_visible_at_exit": data.rfind(b"\x1b[?25h") >= data.rfind(b"\x1b[?25l"),
                "elapsed": time.perf_counter() - start,
                "exit": code,
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        finally:
            os.close(master)
            if slave >= 0:
                os.close(slave)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    entrypoints = parser.add_mutually_exclusive_group(required=True)
    entrypoints.add_argument("--console", type=Path)
    entrypoints.add_argument("--python", type=Path, help="Invoke python -m bernstein.")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--explicit-run", action="store_true")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Start an ordinary idle run and interrupt it after the first meaningful frame.",
    )
    parser.add_argument("--adapter", choices=["auto", "codex"])
    parser.add_argument("--warm-home", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    if os.name != "posix" or options.runs < 1:
        parser.error("this benchmark requires a POSIX terminal and a positive sample count")
    executable = options.console or options.python
    path = str(executable.parent.resolve() / executable.name)
    entry = [path] if options.console else [path, "-m", "bernstein"]
    args = ["run"] if options.explicit_run or options.execute else []
    if options.execute and options.adapter:
        parser.error("the execution fixture uses the idle adapter")
    if options.adapter:
        args.extend(["--cli", options.adapter])
    args.extend(["--idle", "--model", "fixture", "--auto-approve"] if options.execute else ["--plan-only"])
    args.extend(["--goal", _GOAL])
    samples = []
    with tempfile.TemporaryDirectory(prefix="bernstein-tty-home-") as home:
        if options.warm_home:
            measure(entry, args, Path(home), run=options.execute)
        for _ in range(options.runs):
            fixture = contextlib.nullcontext(home) if options.warm_home else tempfile.TemporaryDirectory()
            with fixture as location:
                samples.append(measure(entry, args, Path(location), run=options.execute))
    values = sorted(float(sample["first"]) for sample in samples)
    report = {
        "platform": platform.platform(),
        "python": sys.version,
        "args": args,
        "scenario": "ordinary run interrupted at first feedback" if options.execute else "plan preview",
        "bytecode": "installed interpreter and wheel",
        "home": "warm" if options.warm_home else "empty",
        "samples": samples,
        "median": statistics.median(values),
        "range": [values[0], values[-1]],
    }
    if len(values) >= 50:
        report["p95"] = values[math.ceil(len(values) * 0.95) - 1]
    options.output.write_text(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
