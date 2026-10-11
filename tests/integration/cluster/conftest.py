"""Real-process two-node cluster harness.

Boots two OS processes:
  * ``central`` - uvicorn serving ``bernstein.core.server:app`` with cluster
    mode + JWT auth enabled.
  * ``worker`` - a minimal Python loop that registers, heartbeats, claims,
    and completes tasks. Spawned by the harness, *not* the production
    ``bernstein worker`` CLI (which would pull in the full agent spawner).

The harness only owns sockets, processes, and a Python-level proxy used
to simulate a network partition without root privileges. It is portable
across Linux and macOS; the iptables path described in the original ticket
stays optional and lives behind ``BERNSTEIN_USE_IPTABLES=1`` (Linux+root
only) - the default proxy is sufficient on either platform.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from bernstein.core.security.jwt_tokens import JWTManager, JWTPayload

# --------------------------------------------------------------------------- #
# Tunables
# --------------------------------------------------------------------------- #

# Fast timeouts so a full happy-path scenario completes in well under 15s
# (acceptance criterion). The reaper runs every NODE_HEARTBEAT_S inside the
# central server; node_timeout_s controls when an absent worker is marked
# OFFLINE.
NODE_TIMEOUT_S = 3
HEARTBEAT_INTERVAL_S = 1
SERVER_READY_TIMEOUT_S = 25.0
SERVER_READY_POLL_S = 0.1


def _free_port() -> int:
    """Return an OS-assigned ephemeral TCP port."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return int(port)


def _wait_http_ok(url: str, timeout_s: float = SERVER_READY_TIMEOUT_S) -> bool:
    """Poll ``url`` until it returns 2xx or the deadline expires."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with contextlib.suppress(httpx.HTTPError):
            resp = httpx.get(url, timeout=2.0)
            if 200 <= resp.status_code < 300:
                return True
        time.sleep(SERVER_READY_POLL_S)
    return False


def _terminate(proc: subprocess.Popen[bytes] | None, kill_grace_s: float = 2.0) -> None:
    """Send SIGTERM; escalate to SIGKILL if the child does not exit."""
    if proc is None or proc.poll() is not None:
        return
    with contextlib.suppress(Exception):
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=kill_grace_s)
            return
        try:
            proc.kill()
        except ProcessLookupError:
            return
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=kill_grace_s)


# --------------------------------------------------------------------------- #
# Python-level network partition proxy
# --------------------------------------------------------------------------- #


class _PartitionProxy:
    """Tiny TCP forwarder that can be put into a "drop" mode at runtime.

    The worker connects to the proxy, the proxy forwards to the central
    server. In partition mode the proxy refuses new connections and
    aggressively closes existing ones, so the worker sees real
    connection-refused / read-error exceptions instead of clean responses.

    This is a pure-Python replacement for ``iptables`` so the harness
    works on macOS developer machines as well as Linux CI runners.
    """

    def __init__(self, listen_port: int, target_host: str, target_port: int) -> None:
        self._listen_port = listen_port
        self._target_host = target_host
        self._target_port = target_port
        self._partitioned = False
        self._stopping = False
        self._server_sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._client_sockets: list[socket.socket] = []
        self._lock = threading.Lock()

    @property
    def listen_port(self) -> int:
        return self._listen_port

    def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", self._listen_port))
        sock.listen(64)
        sock.settimeout(0.25)
        self._server_sock = sock
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping = True
        sock = self._server_sock
        self._server_sock = None
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.close()
        with self._lock:
            for cs in self._client_sockets:
                with contextlib.suppress(OSError):
                    cs.close()
            self._client_sockets.clear()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def partition(self) -> None:
        """Drop existing connections and refuse new ones until ``heal()``."""
        self._partitioned = True
        with self._lock:
            for cs in self._client_sockets:
                with contextlib.suppress(OSError):
                    cs.shutdown(socket.SHUT_RDWR)
                with contextlib.suppress(OSError):
                    cs.close()
            self._client_sockets.clear()

    def heal(self) -> None:
        """Resume forwarding."""
        self._partitioned = False

    def _accept_loop(self) -> None:
        while not self._stopping and self._server_sock is not None:
            try:
                client, _addr = self._server_sock.accept()
            except (OSError, TimeoutError):
                continue
            if self._partitioned:
                with contextlib.suppress(OSError):
                    client.close()
                continue
            threading.Thread(target=self._forward, args=(client,), daemon=True).start()

    def _forward(self, client: socket.socket) -> None:
        try:
            upstream = socket.create_connection((self._target_host, self._target_port), timeout=2.0)
        except OSError:
            with contextlib.suppress(OSError):
                client.close()
            return

        with self._lock:
            self._client_sockets.append(client)
            self._client_sockets.append(upstream)

        def pump(src: socket.socket, dst: socket.socket) -> None:
            try:
                src.settimeout(0.5)
                while not self._stopping and not self._partitioned:
                    try:
                        data = src.recv(4096)
                    except (OSError, TimeoutError):
                        if self._partitioned or self._stopping:
                            break
                        continue
                    if not data:
                        break
                    try:
                        dst.sendall(data)
                    except OSError:
                        break
            finally:
                for s in (src, dst):
                    with contextlib.suppress(OSError):
                        s.shutdown(socket.SHUT_RDWR)
                    with contextlib.suppress(OSError):
                        s.close()

        threading.Thread(target=pump, args=(client, upstream), daemon=True).start()
        threading.Thread(target=pump, args=(upstream, client), daemon=True).start()


# --------------------------------------------------------------------------- #
# Cluster JWT helpers
# --------------------------------------------------------------------------- #


def _mint_token(secret: str, node_id: str, *, ttl_s: float = 3600.0, scopes: list[str] | None = None) -> str:
    """Mint a cluster JWT signed with ``secret``.

    Bypasses ``ClusterAuthenticator.issue_node_token`` so we can use a
    sub-hour TTL (needed by the token-expiry scenario).
    """
    if scopes is None:
        scopes = ["node:register", "node:heartbeat", "node:admin"]
    mgr = JWTManager(secret=secret, expiry_hours=1)
    now = time.time()
    payload = JWTPayload(
        session_id=f"node-{node_id}",
        user_id=node_id,
        issued_at=now,
        expires_at=now + ttl_s,
        scopes=scopes,
    )
    return mgr._encode(payload)


# --------------------------------------------------------------------------- #
# Worker subprocess
# --------------------------------------------------------------------------- #


@dataclass
class WorkerHandle:
    """Bookkeeping for one worker process."""

    node_name: str
    proc: subprocess.Popen[bytes]
    log_path: Path
    server_url: str
    secret: str
    node_id: str | None = None
    token: str | None = None


def _worker_script(server_url: str, name: str, secret: str, ready_file: Path, log_path: Path) -> str:
    """Build the inline Python source executed by a worker subprocess.

    The script registers, runs a heartbeat + claim loop, and writes its
    assigned ``node_id`` to ``ready_file`` so the harness can read it back
    deterministically without parsing logs.
    """
    return (
        "import json, os, sys, time, signal\n"
        "import httpx\n"
        "from bernstein.core.security.jwt_tokens import JWTManager, JWTPayload\n"
        "\n"
        f"server={server_url!r}\n"
        f"name={name!r}\n"
        f"secret={secret!r}\n"
        f"ready_file={str(ready_file)!r}\n"
        f"log_path={str(log_path)!r}\n"
        "\n"
        "def _log(msg):\n"
        "    with open(log_path, 'a', encoding='utf-8') as fh:\n"
        "        fh.write(f'{time.time():.3f} {msg}\\n')\n"
        "\n"
        "def _mint(scopes, ttl=3600.0):\n"
        "    mgr = JWTManager(secret=secret, expiry_hours=1)\n"
        "    now = time.time()\n"
        "    payload = JWTPayload(\n"
        "        session_id=f'node-{name}', user_id=name, issued_at=now,\n"
        "        expires_at=now+ttl, scopes=scopes,\n"
        "    )\n"
        "    return mgr._encode(payload)\n"
        "\n"
        "register_token = _mint(['node:register'])\n"
        "register_payload = {\n"
        "    'name': name, 'url': '',\n"
        "    'capacity': {'max_agents': 4, 'available_slots': 4, 'active_agents': 0,\n"
        "                  'gpu_available': False, 'supported_models': ['sonnet']},\n"
        "    'labels': {}, 'cell_ids': [],\n"
        "}\n"
        "node_id = None\n"
        "with httpx.Client() as cli:\n"
        "    for _ in range(100):\n"
        "        try:\n"
        "            r = cli.post(server + '/cluster/nodes', json=register_payload,\n"
        "                          headers={'Authorization': 'Bearer ' + register_token}, timeout=5.0)\n"
        "            if r.status_code == 201:\n"
        "                node_id = r.json()['id']\n"
        "                break\n"
        "        except httpx.HTTPError as exc:\n"
        "            _log(f'register error {exc}')\n"
        "        time.sleep(0.2)\n"
        "    if node_id is None:\n"
        "        _log('register timeout'); sys.exit(2)\n"
        "    with open(ready_file, 'w', encoding='utf-8') as fh:\n"
        "        fh.write(node_id)\n"
        "    _log(f'registered {node_id}')\n"
        "    hb_token = _mint(['node:heartbeat'], ttl=3600.0)\n"
        "    def _stop(_s, _f):\n"
        "        sys.exit(0)\n"
        "    signal.signal(signal.SIGTERM, _stop)\n"
        "    while True:\n"
        "        try:\n"
        "            cli.post(server + f'/cluster/nodes/{node_id}/heartbeat',\n"
        "                      json={'capacity': register_payload['capacity']},\n"
        "                      headers={'Authorization': 'Bearer ' + hb_token}, timeout=5.0)\n"
        "        except httpx.HTTPError as exc:\n"
        "            _log(f'heartbeat error {exc}')\n"
        "        try:\n"
        "            r = cli.get(server + '/tasks/next/backend', timeout=5.0)\n"
        "            if r.status_code == 200:\n"
        "                t = r.json()\n"
        "                _log(f'claimed {t[\"id\"]}')\n"
        "                cli.post(server + f'/tasks/{t[\"id\"]}/complete',\n"
        "                          json={'result_summary': f'done by {name}'}, timeout=5.0)\n"
        "                _log(f'completed {t[\"id\"]}')\n"
        "        except httpx.HTTPError as exc:\n"
        "            _log(f'claim error {exc}')\n"
        "        time.sleep(0.5)\n"
    )


# --------------------------------------------------------------------------- #
# ClusterHandle
# --------------------------------------------------------------------------- #


@dataclass
class ClusterHandle:
    """Public handle returned by the ``two_node_cluster`` fixture.

    All ``central_*`` and ``worker_*`` paths are absolute and live under
    a per-test tmpdir; teardown removes nothing - pytest cleans the tmpdir.
    """

    workdir: Path
    central_port: int
    proxy_port: int
    central_log: Path
    worker_log: Path
    nodes_json: Path
    cluster_secret: str
    central_proc: subprocess.Popen[bytes] | None = None
    proxy: _PartitionProxy | None = None
    workers: list[WorkerHandle] = field(default_factory=list)

    # ---- URLs --------------------------------------------------------- #

    @property
    def central_url(self) -> str:
        """Direct URL to the central server (bypasses proxy)."""
        return f"http://127.0.0.1:{self.central_port}"

    @property
    def proxied_url(self) -> str:
        """URL routed through the partition proxy (workers go through this)."""
        return f"http://127.0.0.1:{self.proxy_port}"

    # ---- helpers ------------------------------------------------------ #

    def admin_token(self, *, ttl_s: float = 3600.0) -> str:
        """Mint a cluster admin token (register + heartbeat + admin)."""
        return _mint_token(self.cluster_secret, "harness-admin", ttl_s=ttl_s)

    def heartbeat_token(self, node_id: str, *, ttl_s: float = 3600.0) -> str:
        """Mint a heartbeat-only token for ``node_id``."""
        return _mint_token(self.cluster_secret, node_id, ttl_s=ttl_s, scopes=["node:heartbeat"])

    def current_status(self) -> dict[str, object]:
        """Return ``GET /cluster/status`` payload from the central server."""
        resp = httpx.get(f"{self.central_url}/cluster/status", timeout=5.0)
        resp.raise_for_status()
        return dict(resp.json())

    # ---- worker control ---------------------------------------------- #

    def start_worker(self, name: str = "worker-alpha") -> WorkerHandle:
        """Spawn a fresh worker subprocess and wait for registration."""
        ready_file = self.workdir / f"ready-{name}.txt"
        if ready_file.exists():
            ready_file.unlink()
        log = self.workdir / f"worker-{name}.log"
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        # Workers always traverse the partition proxy.
        script = _worker_script(self.proxied_url, name, self.cluster_secret, ready_file, log)
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(self.workdir),
        )
        deadline = time.monotonic() + 15.0
        node_id: str | None = None
        while time.monotonic() < deadline:
            if ready_file.exists():
                node_id = ready_file.read_text(encoding="utf-8").strip()
                if node_id:
                    break
            if proc.poll() is not None:
                raise RuntimeError(f"Worker {name!r} exited before registration")
            time.sleep(0.1)
        if node_id is None:
            _terminate(proc)
            raise RuntimeError(f"Worker {name!r} failed to register within 15s")
        handle = WorkerHandle(
            node_name=name,
            proc=proc,
            log_path=log,
            server_url=self.proxied_url,
            secret=self.cluster_secret,
            node_id=node_id,
        )
        self.workers.append(handle)
        return handle

    def kill_worker(self, handle: WorkerHandle, *, force: bool = True) -> None:
        """SIGKILL the worker so its node should be reaped to OFFLINE."""
        if handle.proc.poll() is not None:
            return
        try:
            if force:
                handle.proc.kill()
            else:
                handle.proc.terminate()
        except ProcessLookupError:
            return
        with contextlib.suppress(subprocess.TimeoutExpired):
            handle.proc.wait(timeout=3.0)

    def restart_worker(self, handle: WorkerHandle) -> WorkerHandle:
        """Replace ``handle`` with a freshly spawned worker of the same name."""
        if handle in self.workers:
            self.workers.remove(handle)
        self.kill_worker(handle)
        return self.start_worker(name=handle.node_name)

    def restart_central(self) -> None:
        """SIGTERM central, wait for exit, then re-launch + wait for ready."""
        _terminate(self.central_proc)
        self.central_proc = _start_central(
            workdir=self.workdir,
            port=self.central_port,
            secret=self.cluster_secret,
            log_path=self.central_log,
            nodes_json=self.nodes_json,
        )
        if not _wait_http_ok(f"{self.central_url}/health"):
            raise RuntimeError("Central did not come back up after restart")

    def block_traffic_for(self, seconds: float) -> None:
        """Drop worker<->central traffic for ``seconds`` and then heal."""
        assert self.proxy is not None
        self.proxy.partition()
        time.sleep(seconds)
        self.proxy.heal()

    # ---- teardown ----------------------------------------------------- #

    def shutdown(self) -> None:
        for w in self.workers.copy():
            _terminate(w.proc)
        self.workers.clear()
        _terminate(self.central_proc)
        self.central_proc = None
        if self.proxy is not None:
            self.proxy.stop()
            self.proxy = None


# --------------------------------------------------------------------------- #
# Central process bootstrap
# --------------------------------------------------------------------------- #


def _start_central(
    *,
    workdir: Path,
    port: int,
    secret: str,
    log_path: Path,
    nodes_json: Path,
) -> subprocess.Popen[bytes]:
    """Launch ``uvicorn bernstein.core.server:app`` as a subprocess."""
    env = os.environ.copy()
    env["BERNSTEIN_CLUSTER_ENABLED"] = "1"
    env["BERNSTEIN_CLUSTER_AUTH_SECRET"] = secret
    env["BERNSTEIN_AUTH_DISABLED"] = "1"
    env["BERNSTEIN_CLUSTER_NODE_TIMEOUT_S"] = str(NODE_TIMEOUT_S)
    env["BERNSTEIN_CLUSTER_HEARTBEAT_INTERVAL_S"] = str(HEARTBEAT_INTERVAL_S)
    env["BERNSTEIN_BIND_HOST"] = "127.0.0.1"
    env["PYTHONUNBUFFERED"] = "1"
    # Persist node registry to a known path so restart-from-disk is testable.
    # The server auto-detects ``.sdd/runtime/nodes.json`` from the jsonl path,
    # so we set up the directory layout it expects.
    runtime_dir = nodes_json.parent
    runtime_dir.mkdir(parents=True, exist_ok=True)
    log_fh = log_path.open("a", encoding="utf-8")
    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "bernstein.core.server:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--log-level",
        "warning",
    ]
    proc = subprocess.Popen(
        cmd,
        env=env,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        cwd=str(workdir),
        start_new_session=True,
    )
    log_fh.close()
    return proc


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def two_node_cluster(tmp_path: Path) -> Iterator[ClusterHandle]:
    """Boot a real two-process cluster and tear it down on exit.

    The fixture only allocates ports + spawns the central server. Tests
    call ``handle.start_worker(...)`` to bring workers online - that gives
    each scenario explicit control over how many nodes participate.
    """
    central_port = _free_port()
    proxy_port = _free_port()
    workdir = tmp_path
    sdd_runtime = workdir / ".sdd" / "runtime"
    sdd_runtime.mkdir(parents=True, exist_ok=True)
    nodes_json = sdd_runtime / "nodes.db"
    central_log = workdir / "central.log"
    worker_log = workdir / "worker.log"
    cluster_secret = secrets.token_urlsafe(32)

    proxy = _PartitionProxy(
        listen_port=proxy_port,
        target_host="127.0.0.1",
        target_port=central_port,
    )
    proxy.start()

    central_proc = _start_central(
        workdir=workdir,
        port=central_port,
        secret=cluster_secret,
        log_path=central_log,
        nodes_json=nodes_json,
    )

    if not _wait_http_ok(f"http://127.0.0.1:{central_port}/health"):
        try:
            log_text = central_log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            log_text = "<no central log>"
        _terminate(central_proc)
        proxy.stop()
        raise RuntimeError(f"Central server failed to become ready.\n--- central.log ---\n{log_text[-2000:]}")

    handle = ClusterHandle(
        workdir=workdir,
        central_port=central_port,
        proxy_port=proxy_port,
        central_log=central_log,
        worker_log=worker_log,
        nodes_json=nodes_json,
        cluster_secret=cluster_secret,
        central_proc=central_proc,
        proxy=proxy,
    )

    failure_diag: dict[str, object] = {}
    try:
        yield handle
    except BaseException:
        # Capture forensic state before teardown for the test-failure report.
        try:
            failure_diag["status"] = handle.current_status()
        except Exception:
            failure_diag["status"] = "unavailable"
        try:
            failure_diag["nodes_json"] = "<present>" if nodes_json.exists() else "<missing>"
        except OSError:
            failure_diag["nodes_json"] = "<read-error>"
        try:
            failure_diag["central_log_tail"] = central_log.read_text(encoding="utf-8", errors="replace").splitlines()[
                -100:
            ]
        except OSError:
            failure_diag["central_log_tail"] = []
        sys.stderr.write("\n--- two_node_cluster diagnostics ---\n")
        sys.stderr.write(json.dumps(failure_diag, indent=2, default=str)[:6000])
        sys.stderr.write("\n--- end diagnostics ---\n")
        raise
    finally:
        handle.shutdown()


# --------------------------------------------------------------------------- #
# Skip the suite cleanly when running on platforms / environments where the
# real subprocess path isn't viable. Currently we only block Windows because
# uvicorn + signal semantics differ enough that the harness would need a
# Windows-specific path nobody is going to run.
# --------------------------------------------------------------------------- #

if sys.platform == "win32":  # pragma: no cover - never executed on the supported targets
    collect_ignore_glob = ["test_real_2node.py"]


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip ``cluster_e2e`` tests unless the user opted in.

    Opt-in signals (any one is enough):
      * ``-m cluster_e2e`` on the CLI
      * ``--run-cluster-e2e`` flag on the CLI
      * ``BERNSTEIN_RUN_CLUSTER_E2E=1`` env var
    """
    markexpr = getattr(config.option, "markexpr", "") or ""
    if "cluster_e2e" in markexpr:
        return
    if config.getoption("--run-cluster-e2e", default=False):
        return
    if os.environ.get("BERNSTEIN_RUN_CLUSTER_E2E", "").lower() in ("1", "true", "yes"):
        return
    skip_marker = pytest.mark.skip(
        reason="cluster_e2e suite is opt-in: pass -m cluster_e2e or --run-cluster-e2e",
    )
    for item in items:
        if any(m.name == "cluster_e2e" for m in item.iter_markers()):
            item.add_marker(skip_marker)


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register the ``--run-cluster-e2e`` opt-in flag."""
    group = parser.getgroup("bernstein-cluster")
    group.addoption(
        "--run-cluster-e2e",
        action="store_true",
        default=False,
        help="Run the real-process cluster e2e suite (otherwise auto-skipped).",
    )


# Re-export so test modules get a stable import path.
__all__ = [
    "ClusterHandle",
    "WorkerHandle",
    "_PartitionProxy",
    "_mint_token",
    "two_node_cluster",
]


# Ensure pytest cleans the SIGTERM handler on Ctrl-C in long-running scenarios.
def pytest_keyboard_interrupt(excinfo: pytest.ExceptionInfo[BaseException]) -> None:  # pragma: no cover
    del excinfo
    signal.signal(signal.SIGINT, signal.SIG_DFL)
