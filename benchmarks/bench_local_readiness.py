"""Time an installed local run from process launch to an authenticated dashboard response.

Use the benchmark on a declared reference machine. Each run uses an empty
application home and a new Git project, a loopback port and the idle adapter.
It never invokes an external agent or fetches an optional remote catalog.
"""

from __future__ import annotations

import argparse
import contextlib
import http.client
import json
import math
import os
import platform
import shlex
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import psutil


def _json(port: int, path: str, token: str) -> dict[str, Any]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=0.25)
    try:
        conn.request("GET", path, headers={"Authorization": f"Bearer {token}"})
        response = conn.getresponse()
        data = response.read()
        return json.loads(data) if response.status == 200 else {}
    except (OSError, http.client.HTTPException, ValueError):
        return {}
    finally:
        conn.close()


def _stop(project: Path, owner: psutil.Process) -> None:
    # The CLI detaches its server, spawner and watchdog. Each writes its PID
    # into this new fixture. Check the working directory before signalling.
    roots = {owner}
    for name in ("server", "spawner", "watchdog"):
        with contextlib.suppress(OSError, ValueError, psutil.NoSuchProcess):
            pid = int((project / f".sdd/runtime/{name}.pid").read_text())
            process = psutil.Process(pid)
            if Path(process.cwd()).resolve() == project.resolve():
                roots.add(process)
    children = roots.copy()
    for root in roots:
        with contextlib.suppress(psutil.NoSuchProcess):
            children.update(root.children(recursive=True))
    for child in children:
        with contextlib.suppress(psutil.NoSuchProcess):
            child.terminate()
    _, running = psutil.wait_procs(children, timeout=2)
    for child in running:
        with contextlib.suppress(psutil.NoSuchProcess):
            child.kill()
    psutil.wait_procs(running, timeout=3)


def sample(console: Path, *, app_home: Path | None = None, auto_routing: bool = False) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="bernstein-bench-") as location:
        fixture = Path(location)
        project = fixture / "project"
        project.mkdir()
        user = app_home or fixture
        for path in ("home", "cache", "config", "data"):
            (user / path).mkdir(exist_ok=True)
        token = uuid.uuid4().hex
        env = {
            "PATH": str(console.parent) + os.pathsep + os.defpath,
            "HOME": str(user / "home"),
            "USERPROFILE": str(user / "home"),
            "XDG_CONFIG_HOME": str(user / "config"),
            "XDG_CACHE_HOME": str(user / "cache"),
            "XDG_DATA_HOME": str(user / "data"),
            "BERNSTEIN_AUTH_TOKEN": token,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_ALLOW_PROTOCOL": "file",
            "GIT_TERMINAL_PROMPT": "0",
            "TERM": "dumb",
        }
        for variable in ("SYSTEMROOT", "WINDIR"):
            if variable in os.environ:
                env[variable] = os.environ[variable]
        git = shutil.which("git")
        if git is None:
            raise RuntimeError("git must be installed")
        for args in (
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
            subprocess.run([git, *args], cwd=project, env=env, check=True, capture_output=True)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        cmd = [
            str(console),
            "run",
            "--idle",
            "--model",
            "fixture",
            "--goal",
            "Local fixture",
            "--port",
            str(port),
            "--auto-approve",
        ]
        marker = fixture / "fixture-agent-started"
        if auto_routing:
            # Exercise real discovery, routing and process launch. Only our
            # local executable is on the adapter path; it cannot call a provider.
            if os.name != "posix":
                raise RuntimeError("the auto-routing fixture requires POSIX")
            binary_dir = fixture / "bin"
            binary_dir.mkdir()
            binary = binary_dir / "claude"
            binary.write_text(
                "#!/bin/sh\n"
                'if [ "$1" = "--version" ]; then echo "claude 1.0.0"; exit 0; fi\n'
                'case " $* " in\n'
                '  *" say ok "*) echo ok; exit 0 ;;\n'
                f'  *" --strict-mcp-config "*) touch {shlex.quote(str(marker))}; exec sleep 25 ;;\n'
                '  *) exit 0 ;;\n'
                "esac\n"
            )
            binary.chmod(0o755)
            env["PATH"] = str(binary_dir) + os.pathsep + env["PATH"]
            (user / "home" / ".claude").mkdir(exist_ok=True)
            (project / "bernstein.yaml").write_text("goal: Local fixture\ncli: auto\nmodel: fixture\n")
            cmd.remove("--idle")
            cmd.extend(["--cli", "auto"])
        env["BERNSTEIN_SERVER_URL"] = f"http://127.0.0.1:{port}"
        timings: dict[str, Any] = {}
        with (fixture / "console.log").open("w+b") as output:
            start = time.perf_counter()
            proc = subprocess.Popen(
                cmd, cwd=project, env=env, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT
            )
            owner = psutil.Process(proc.pid)
            try:
                while time.perf_counter() - start < 30:
                    if "server_spawned" not in timings and (project / ".sdd/runtime/server.pid").is_file():
                        timings["server_spawned"] = time.perf_counter() - start
                    if "health" not in timings and _json(port, "/health/ready", token).get("status") == "ready":
                        timings["health"] = time.perf_counter() - start
                    if "health" in timings and "first_agent_ready" not in timings:
                        dashboard = _json(port, "/dashboard/data", token)
                        if {"stats", "tasks", "agents"} <= dashboard.keys():
                            timings.setdefault("ready", time.perf_counter() - start)
                            for agent in dashboard["agents"]:
                                timings.setdefault("first_agent_registration", time.perf_counter() - start)
                                if agent.get("status") in {"working", "idle"}:
                                    timings["first_agent_ready"] = time.perf_counter() - start
                                    timings["agent_status"] = agent["status"]
                                    break
                    if proc.poll() is not None:
                        break
                    time.sleep(0.05)
                if auto_routing:
                    timings["local_adapter_invoked"] = marker.exists()
                timings["exit"] = proc.poll()
                timings["elapsed"] = time.perf_counter() - start
                if timings["exit"] != 0 or "first_agent_ready" not in timings:
                    output.seek(0)
                    timings["diagnostic"] = output.read().decode(errors="replace")[-1500:]
            finally:
                _stop(project, owner)
                proc.wait(timeout=5)
        return timings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--console", type=Path, required=True)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--warm-home", action="store_true", help="Prime and reuse user caches, keeping each project fresh."
    )
    parser.add_argument(
        "--auto-routing", action="store_true", help="Exercise auto routing with an isolated local CLI fixture."
    )
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be positive")
    results: list[dict[str, Any]] = []
    report = {
        "platform": platform.platform(),
        "python": sys.version,
        "app_home": "primed" if args.warm_home else "empty",
        "bytecode": "installed interpreter and wheel",
        "samples": results,
    }
    with tempfile.TemporaryDirectory(prefix="bernstein-bench-home-") as location:
        app_home = Path(location) if args.warm_home else None
        console = args.console.resolve()
        if app_home is not None:
            prime = sample(console, app_home=app_home, auto_routing=args.auto_routing)
            if prime["exit"] != 0 or "first_agent_ready" not in prime:
                raise RuntimeError(f"Could not prime application home: {prime}")
        for _ in range(args.runs):
            measurement = sample(console, app_home=app_home, auto_routing=args.auto_routing)
            results.append(measurement)
            args.output.write_text(json.dumps(report, indent=2) + "\n")
            if (
                measurement["exit"] != 0
                or "first_agent_ready" not in measurement
                or (args.auto_routing and not measurement["local_adapter_invoked"])
            ):
                return 1
    for key in ("health", "ready", "first_agent_registration", "first_agent_ready"):
        values = sorted(s[key] for s in results)
        print(f"{key} n={len(values)} median={statistics.median(values):.3f}s range={values[0]:.3f}..{values[-1]:.3f}s")
        if len(values) >= 50:
            print(f"{key} p95={values[math.ceil(0.95 * len(values)) - 1]:.3f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
