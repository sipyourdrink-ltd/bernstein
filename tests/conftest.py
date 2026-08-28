"""Shared pytest fixtures for the bernstein test suite."""

from __future__ import annotations

import gc
import os
import platform
import subprocess
import sys

# Optional runtime type-check hook. No-op unless BEARTYPE_USE_CLAW is set
# in the environment; CI's `beartype` job opts in explicitly.
from tests._beartype_claw import maybe_install_beartype_claw

maybe_install_beartype_claw()


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register custom CLI options for the bernstein test suite."""
    parser.addoption(
        "--live",
        action="store_true",
        default=False,
        help="Run live adapter conformance tests against real installed binaries.",
    )


from collections.abc import Callable, Iterator
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from bernstein.core.adaptive_parallelism import AdaptiveParallelism
from bernstein.core.models import (
    Complexity,
    ModelConfig,
    OrchestratorConfig,
    Scope,
    Task,
    TaskStatus,
    TaskType,
)
from bernstein.core.orchestrator import Orchestrator
from bernstein.core.spawner import AgentSpawner
from fastapi.testclient import TestClient

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult
from bernstein.core.server import create_app

if TYPE_CHECKING:
    from fastapi import FastAPI

# ---------------------------------------------------------------------------
# Memory guard: prevent any single pytest run from eating >2 GB RAM.
# ---------------------------------------------------------------------------


def _memory_guard_bytes() -> int:
    """RSS cap for a pytest run (default 2 GB).

    The rendering lane (.github/workflows/rendering-lane.yml) launches a
    headless Chromium, which reserves a ~1.5 TB *virtual* address space
    (V8 cage + ASLR; the mappings do not consume RAM, but RLIMIT_AS counts
    them, so the 2 GB guard kills it at launch). That lane raises the
    ceiling up front via ``BERNSTEIN_MEM_GUARD_GB`` (2048); everything
    else keeps the 2 GB guard. A value of 0 keeps the default guard.
    """
    _mem_guard_gb = os.environ.get("BERNSTEIN_MEM_GUARD_GB")
    if _mem_guard_gb is not None:
        try:
            _gb = max(int(_mem_guard_gb), 0)
        except ValueError:
            _gb = 0
        if _gb > 0:
            return _gb * 1024 * 1024 * 1024
    return 2 * 1024 * 1024 * 1024  # 2 GB


_MAX_RSS_BYTES = _memory_guard_bytes()

#: Force a full collection only once RSS has climbed past this share of the
#: cap. ``gc.collect()`` walks the whole live heap, so collecting after every
#: test made a file's teardown cost scale with the object graph that file
#: builds. On ``tests/unit/test_tenant_scope_http_isolation.py`` -- 137 tests,
#: each building a full app, ~4.5M live objects by the end -- the collections
#: alone accounted for 84s of the file's 201s, which put it over the per-file
#: subprocess budget on slower hosts. Below the watermark there is nothing
#: worth reclaiming and the generational collector is already running.
_GC_WATERMARK_BYTES = _MAX_RSS_BYTES // 2

_SPAWNER_TMP_REPO_TESTS = {
    "test_adapter_model_default.py",
    "test_agent_signals.py",
    "test_approval_gates.py",
    "test_conflict_resolution.py",
    "test_coordination.py",
    "test_crash_recovery.py",
    "test_evolution_integration.py",
    "test_evolve_mode.py",
    "test_failure_reduction.py",
    "test_idle_agent_detection.py",
    "test_manager_write_boundary.py",
    "test_mcp_config.py",
    "test_mcp_manager.py",
    "test_mcp_registry.py",
    "test_oauth_refresh.py",
    "test_orchestrator.py",
    "test_orchestrator_batch_ingest.py",
    "test_prompt_caching.py",
    "test_regression_orchestrator.py",
    "test_spawner.py",
    "test_spawner_openclaw_bridge.py",
    "test_spawner_sandbox.py",
    "test_unattended_retry.py",
    "test_wal_recovery.py",
    "test_workspace.py",
}

if platform.system() != "Windows":
    import resource

    with suppress(ValueError, AttributeError):
        _soft, _hard = resource.getrlimit(resource.RLIMIT_AS)
        resource.setrlimit(resource.RLIMIT_AS, (_MAX_RSS_BYTES, _hard))


def _current_rss_bytes() -> int:
    """Return the process's current resident set size in bytes.

    Deliberately the *current* RSS, not ``ru_maxrss``: the latter is the
    lifetime peak and never decreases, so once any single test spiked past
    the cap every later teardown in the run would trip the guard even
    though the memory had long been reclaimed.
    """
    import psutil

    return int(psutil.Process().memory_info().rss)


def _enforce_memory_guard(rss_bytes: int) -> None:
    """Abort the pytest session once if live RSS exceeds the cap.

    Uses ``pytest.exit`` rather than ``sys.exit``: ``SystemExit`` raised in
    a fixture teardown is recorded as a per-test ERROR and the run keeps
    going, so a single crossing used to cascade into an error on every
    remaining test in the session. ``pytest.exit`` stops the run exactly
    once with a clear message.
    """
    if rss_bytes > _MAX_RSS_BYTES:
        pytest.exit(
            f"pytest RSS exceeded {_MAX_RSS_BYTES // (1024**3)} GB (actual: {rss_bytes / (1024**3):.1f} GB). Aborting.",
            returncode=137,
        )


def _run_memory_guard(
    rss_probe: Callable[[], int],
    collect: Callable[[], None],
    enforce: Callable[[int], None],
) -> None:
    """Reclaim only when it can matter, then enforce the cap on what is left.

    Collecting unconditionally cost every test the price of walking the whole
    live heap, whether or not anything was reclaimable. Reading RSS first and
    collecting only above the watermark keeps the reclamation where it does
    work; re-reading afterwards means the cap is enforced against the RSS that
    survived the collection, not the pre-collection figure.
    """
    rss_bytes = rss_probe()
    if rss_bytes >= _GC_WATERMARK_BYTES:
        collect()
        rss_bytes = rss_probe()
    enforce(rss_bytes)


def _memory_guard_teardown(system: str) -> None:
    """Run the guard only on the platform whose cap it enforces.

    ``_current_rss_bytes`` asks psutil for the process's memory info, which on
    Linux means reading ``/proc``. The cap has only ever been enforced on
    Darwin, so probing anywhere else buys nothing and puts a filesystem read
    into the teardown of every test -- including the purity suites that assert
    a resolver touched no clock, filesystem, or network.
    """
    if system != "Darwin":
        return
    _run_memory_guard(_current_rss_bytes, gc.collect, _enforce_memory_guard)


@pytest.fixture(autouse=True)
def _no_git_background_maintenance(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop every git the suite starts from running background housekeeping.

    ``git commit`` may hand off to ``git maintenance``, which writes and then
    unlinks ``.git/objects/maintenance.lock`` on its own schedule. A helper
    that walks or deletes ``.git`` right after committing races that unlink
    and dies with ``FileNotFoundError: 'maintenance.lock'``. Pinning the
    config through the environment reaches every git process the suite
    starts, including helpers that never call ``git config`` themselves.
    """
    start = int(os.environ.get("GIT_CONFIG_COUNT", "0"))
    for offset, (key, value) in enumerate((("gc.auto", "0"), ("maintenance.auto", "false"))):
        monkeypatch.setenv(f"GIT_CONFIG_KEY_{start + offset}", key)
        monkeypatch.setenv(f"GIT_CONFIG_VALUE_{start + offset}", value)
    monkeypatch.setenv("GIT_CONFIG_COUNT", str(start + 2))


@pytest.fixture(autouse=True)
def _memory_guard():
    """Reclaim as RSS approaches the cap; abort the session once it exceeds it."""
    yield
    _memory_guard_teardown(platform.system())


@pytest.fixture(autouse=True)
def _stable_adaptive_parallelism(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep adaptive parallelism deterministic across the test suite.

    Integration tests should not depend on ambient machine load. Individual
    adaptive-parallelism tests can still override this with their own patches.
    """

    monkeypatch.setattr(AdaptiveParallelism, "_get_cpu_percent", lambda self: 0.0)


@pytest.fixture(autouse=True)
def _isolate_audit_key(
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Point the audit HMAC key path at a per-test tmpdir.

    Without this, ``AuditLog(...)`` calls that omit ``key=`` would read or
    create a file at ``~/.local/state/bernstein/audit.key`` - polluting the
    developer's home directory with state from the test run. Tests that
    specifically exercise key-path resolution opt out via
    ``pytest.mark.audit_key_real``.
    """

    if request.node.get_closest_marker("audit_key_real") is not None:
        return
    key_path = tmp_path_factory.mktemp("audit-key") / "audit.key"
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(key_path))


@pytest.fixture(autouse=True)
def _disable_auth_for_tests(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable Bernstein auth by default in the test suite.

    Production default is "auth enabled".  Existing tests assume "no bearer
    token needed"; rather than thread a token through dozens of fixtures we
    set ``BERNSTEIN_AUTH_DISABLED=1`` for every test.  Tests that exercise
    the auth behaviour itself (see ``tests/unit/test_auth_middleware_defaults.py``)
    mark themselves with ``pytest.mark.auth_enabled`` to opt out.
    """

    if request.node.get_closest_marker("auth_enabled") is not None:
        # Remove the env var so the middleware sees a "secure-by-default" env.
        monkeypatch.delenv("BERNSTEIN_AUTH_DISABLED", raising=False)
        return
    monkeypatch.setenv("BERNSTEIN_AUTH_DISABLED", "1")


@pytest.fixture(autouse=True)
def _isolate_agent_card_keystore(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Point the persistent A2A v1.0 agent-card keystore at a per-test tmpdir.

    The keystore (``bernstein.core.routes.well_known._KEYSTORE``) is process
    global - without isolation, a single test that pointed it at a
    ``tmp_path`` directory would leave the cache holding a now-deleted path
    once pytest cleaned the dir up. Subsequent tests would then hang on the
    first ``/.well-known/agent.json`` GET as the keystore tried to read its
    vanished private key.

    Override the env var so every test gets a fresh, durable directory in
    the per-session tmpdir, and explicitly reset the in-process cache so
    older tests' bindings don't bleed through.
    """
    key_dir = tmp_path_factory.mktemp("agent-card-keys")
    monkeypatch.setenv("BERNSTEIN_AGENT_CARD_KEY_DIR", str(key_dir))
    # Reset both the global keystore binding and the cached PEM bytes so the
    # next call to ``_get_signing_keypair`` re-binds to the freshly-set env.
    from bernstein.core.routes import well_known as _wk

    _wk._reset_signing_keypair_for_tests(key_dir)


_NETWORK_POSTURE_ENV_VARS = (
    "BERNSTEIN_PROFILE_MODE",
    "BERNSTEIN_NETWORK_POLICY",
    "BERNSTEIN_SOVEREIGN_MODE",
)


@pytest.fixture(autouse=True)
def _restore_network_posture_env() -> Iterator[None]:
    """Put the profile / policy / sovereign env vars back after every test.

    ``install_policy()`` and the sovereign branch of ``_install_network_policy()``
    write straight to ``os.environ`` on purpose - spawned adapters inherit the
    posture that way - and there is no uninstall counterpart. Tests that call
    those installers clean up with a ``monkeypatch.delenv(..., raising=False)``
    preamble, which looks like it covers the case but does not:
    ``MonkeyPatch.delitem`` only records an undo entry when the key is already
    present, so on a clean environment it registers nothing and the values
    written *afterwards* by the installer survive the test.

    A leaked ``BERNSTEIN_SOVEREIGN_MODE=1`` makes ``is_sovereign_profile()`` true
    for the rest of the session, which turns the spawner's posture-drift
    preflight from a no-op into a hard refusal for every later test whose
    workspace has no attestation. Snapshot and restore here so the trap is
    closed for any test that touches these vars, however it sets them.
    """
    saved = {name: os.environ.get(name) for name in _NETWORK_POSTURE_ENV_VARS}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@pytest.fixture(autouse=True)
def _init_git_repo_for_spawner_tmp_path_tests(request: pytest.FixtureRequest) -> None:
    """Initialize a minimal git repo for AgentSpawner tests that use ``tmp_path``.

    Bernstein's spawner defaults to git worktree isolation. A subset of unit tests
    exercises prompt/bulletin/router behavior on ``tmp_path`` without explicitly
    disabling worktrees, so they need a committed repo root to be valid.
    """

    if "tmp_path" not in request.fixturenames:
        return

    test_file = Path(str(request.node.fspath)).name
    if test_file not in _SPAWNER_TMP_REPO_TESTS:
        return

    tmp_path = request.getfixturevalue("tmp_path")
    if (tmp_path / ".git").exists():
        return

    subprocess.run(["git", "init", "-b", "main"], cwd=str(tmp_path), capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=str(tmp_path), capture_output=True, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(tmp_path), capture_output=True, check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=str(tmp_path), capture_output=True, check=True)


def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None) -> None:
    """Aggressively clear references that pytest holds onto after each test."""
    # Clear funcargs/fixtures that may hold large mock objects or tmp_path data
    if hasattr(item, "funcargs"):
        item.funcargs.clear()
    # Clear report sections (captured stdout/stderr per test)
    if hasattr(item, "_report_sections"):
        item._report_sections.clear()
    gc.collect()


@pytest.fixture
def make_task() -> Callable[..., Task]:
    """Factory fixture for Task objects with sensible defaults.

    Supports all common Task fields; tests override only what they care about.
    """

    def _factory(
        *,
        id: str = "T-001",
        role: str = "backend",
        title: str = "Implement feature",
        description: str = "Write the code.",
        scope: Scope = Scope.MEDIUM,
        complexity: Complexity = Complexity.MEDIUM,
        status: TaskStatus = TaskStatus.OPEN,
        task_type: TaskType = TaskType.STANDARD,
        priority: int = 2,
        owned_files: list[str] | None = None,
        mcp_servers: list[str] | None = None,
    ) -> Task:
        return Task(
            id=id,
            title=title,
            description=description,
            role=role,
            scope=scope,
            complexity=complexity,
            status=status,
            task_type=task_type,
            priority=priority,
            owned_files=owned_files or [],
            mcp_servers=mcp_servers or [],
        )

    return _factory


@pytest.fixture
def mock_adapter_factory() -> Callable[..., MagicMock]:
    """Factory fixture for CLIAdapter mocks with configurable PID."""

    def _factory(pid: int = 42) -> MagicMock:
        adapter = MagicMock(spec=CLIAdapter)
        adapter.spawn.return_value = SpawnResult(pid=pid, log_path=Path("/tmp/test.log"))
        adapter.is_alive.return_value = True
        adapter.is_rate_limited.return_value = False
        adapter.kill.return_value = None
        adapter.name.return_value = "MockCLI"
        return adapter

    return _factory


@pytest.fixture
def sdd_dir(tmp_path: Path) -> Path:
    """Temporary .sdd directory with standard subdirectories pre-created."""
    sdd = tmp_path / ".sdd"
    (sdd / "backlog" / "open").mkdir(parents=True)
    (sdd / "backlog" / "done").mkdir(parents=True)
    (sdd / "runtime").mkdir(parents=True)
    (sdd / "metrics").mkdir(parents=True)
    (sdd / "upgrades").mkdir(parents=True)
    return sdd


# ---------------------------------------------------------------------------
# Integration & Chaos Engineering Fixtures
# ---------------------------------------------------------------------------


class IntegrationMockAdapter(CLIAdapter):
    """A flexible mock adapter that executes python commands from task descriptions."""

    default_model = "mock"

    def __init__(self, sdd_path: Path):
        self.sdd_path = sdd_path

    def spawn(
        self,
        *,
        prompt: str,
        workdir: Path,
        model_config: ModelConfig,
        session_id: str,
        mcp_config: dict[str, Any] | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        task_scope: str = "medium",
        budget_multiplier: float = 1.0,
        system_addendum: str = "",
    ) -> SpawnResult:
        log_path = workdir / ".sdd" / "runtime" / f"agent-{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        # Extract python script if present
        script_body = ""
        if "```python" in prompt and "# INTEGRATION-MOCK" in prompt:
            parts = prompt.split("```python")
            for part in parts[1:]:
                code = part.split("```")[0]
                if "# INTEGRATION-MOCK" in code:
                    script_body = code
                    break

        if not script_body:
            # Default: just commit and write a marker file for conftest to pick up
            import re

            task_ids = re.findall(r"id=([A-Za-z0-9\-_]+)", prompt)

            marker_dir = self.sdd_path.resolve() / "runtime"
            marker_dir.mkdir(parents=True, exist_ok=True)

            markers_lines = "\n".join(
                f"    (Path('{marker_dir}') / 'DONE_{tid}').write_text('done')" for tid in task_ids
            )

            script_body = f"""import os
import subprocess
import sys
import time
from pathlib import Path

print(f"Mock agent starting (PID {{os.getpid()}})...")
print(f"Workdir: {{os.getcwd()}}")
# Give orchestrator plenty of time to see us alive
time.sleep(2.0)

# Mock work
try:
    with open("mock_output.txt", "w") as f:
        f.write("completed {session_id}")
    print("Wrote mock_output.txt")

    # Git ops
    res = subprocess.run(["git", "add", "."], cwd="{workdir}", check=False, capture_output=True, text=True)
    print(f"git add: {{res.returncode}} {{res.stdout}} {{res.stderr}}")

    res = subprocess.run(["git", "commit", "-m", "mock work"], cwd="{workdir}", check=False, capture_output=True, text=True)
    print(f"git commit: {{res.returncode}} {{res.stdout}} {{res.stderr}}")

    # Completion markers
    print(f"Writing markers to {marker_dir}...")
{markers_lines}
    print("Wrote markers successfully")
    time.sleep(1.0)
except Exception as e:
    print(f"Error in mock script: {{e}}", file=sys.stderr)
    import traceback
    traceback.print_exc()
    sys.exit(1)
"""

        script_path = self.sdd_path / "runtime" / f"script-{session_id}.py"
        script_path.write_text(script_body)

        with open(log_path, "w") as f:
            proc = subprocess.Popen(
                [sys.executable, str(script_path)],
                stdout=f,
                stderr=subprocess.STDOUT,
                cwd=str(workdir),
            )

        return SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)

    def name(self) -> str:
        return "integration-mock"


@pytest.fixture
def integration_sdd(tmp_path: Path) -> Path:
    # Clear env vars that might affect the run
    for key in list(os.environ.keys()):
        if key.startswith("BERNSTEIN_"):
            del os.environ[key]

    sdd = tmp_path / ".sdd"
    (sdd / "runtime").mkdir(parents=True)
    (sdd / "backlog" / "open").mkdir(parents=True)
    (sdd / "backlog" / "done").mkdir(parents=True)
    (sdd / "metrics").mkdir(parents=True)
    (sdd / "config").mkdir(parents=True)
    (sdd / "incidents").mkdir(parents=True)

    for role in ["backend", "frontend", "manager"]:
        templates = tmp_path / "templates" / "roles" / role
        templates.mkdir(parents=True, exist_ok=True)
        (templates / "system_prompt.md").write_text(f"You are a {role} specialist.")

    # Init git repo in tmp_path
    subprocess.run(["git", "init", "-b", "main"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(tmp_path), check=True)
    (tmp_path / "README.md").write_text("# Test Project")
    subprocess.run(["git", "add", "README.md"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=str(tmp_path), check=True)

    return sdd


@pytest.fixture
def test_app(integration_sdd: Path) -> FastAPI:
    import os

    os.environ.setdefault("BERNSTEIN_AUTH_DISABLED", "1")
    jsonl_path = integration_sdd / "runtime" / "tasks.jsonl"
    return create_app(jsonl_path=jsonl_path)


@pytest.fixture
def test_client(test_app: FastAPI) -> TestClient:
    return TestClient(test_app)


@pytest.fixture
def orchestrator_factory(integration_sdd: Path):
    def _create(max_agents: int = 1, use_worktrees: bool = False):
        os.environ["BERNSTEIN_CLI"] = "integration-mock"
        os.environ["BERNSTEIN_MAX_TASK_RETRIES"] = "0"
        os.environ["BERNSTEIN_HEARTBEAT_TIMEOUT"] = "60"
        os.environ["BERNSTEIN_ADAPTER_ADMISSION_POLICY"] = "off"
        os.environ["BERNSTEIN_ALLOW_MERGE_TO_DEFAULT_BRANCH"] = "1"

        config = OrchestratorConfig(
            server_url="http://127.0.0.1:8052",
            max_agents=max_agents,
            poll_interval_s=1,
            max_task_retries=0,
            max_tasks_per_agent=1,
        )

        from bernstein.adapters.registry import register_adapter

        adapter = IntegrationMockAdapter(integration_sdd)
        register_adapter("integration-mock", adapter)

        from bernstein.core.agents.spawn_rate_limiter import (
            SpawnRateLimitConfig,
            SpawnRateLimiter,
        )

        permissive_rate_limiter = SpawnRateLimiter(SpawnRateLimitConfig(max_spawns=1000, window_seconds=1.0))

        spawner = AgentSpawner(
            adapter=adapter,
            templates_dir=integration_sdd.parent / "templates" / "roles",
            workdir=integration_sdd.parent,
            use_worktrees=use_worktrees,
            spawn_rate_limiter=permissive_rate_limiter,
            default_model="mock-model",
        )
        orchestrator = Orchestrator(config, spawner, workdir=integration_sdd.parent)
        orchestrator._adaptive_parallelism.effective_max_agents = lambda: config.max_agents  # type: ignore[method-assign]
        return orchestrator

    return _create
