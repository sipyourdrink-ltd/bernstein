"""Scope of the suite's ``RLIMIT_AS`` guard (#5938).

``tests/conftest.py`` caps the address space at ``BERNSTEIN_MEM_GUARD_GB``
(2 GB by default). It used to do that at import time, in every process that
loaded the conftest, which included the pytest-xdist controller. The
controller runs no tests, but its address space grows with the session, and
past the cap ``pthread_create`` fails and the run dies at teardown with
``can't start new thread``. The cap now applies in workers and in plain runs
only, and a failure the cap can cause names the cap.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests import conftest as suite_conftest

REPO_ROOT = Path(__file__).resolve().parents[2]

posix_only = pytest.mark.skipif(platform.system() == "Windows", reason="RLIMIT_AS is POSIX-only")


def _platform_lowers_rlimit_as() -> bool:
    """Whether this kernel accepts a 2 GB ``RLIMIT_AS`` (Linux does; macOS refuses with ValueError)."""
    if platform.system() == "Windows":
        return False
    probe = (
        "import resource\n"
        "_, hard = resource.getrlimit(resource.RLIMIT_AS)\n"
        "resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, hard))\n"
    )
    return subprocess.run([sys.executable, "-c", probe], capture_output=True, check=False).returncode == 0


#: The real-session tests read the limit back, so they need a kernel that sets it.
honours_rlimit_as = pytest.mark.skipif(
    not _platform_lowers_rlimit_as(), reason="this platform does not lower RLIMIT_AS, so the guard is inert here"
)


def _config(*, dist: str | None, worker: bool) -> Any:
    option = SimpleNamespace() if dist is None else SimpleNamespace(dist=dist)
    config = SimpleNamespace(option=option)
    if worker:
        config.workerinput = {"workerid": "gw0"}
    return config


@pytest.mark.parametrize(
    ("dist", "worker", "expected"),
    [
        pytest.param("load", False, True, id="xdist-controller"),
        pytest.param("loadfile", False, True, id="xdist-controller-loadfile"),
        pytest.param("load", True, False, id="xdist-worker"),
        pytest.param("no", False, False, id="plain-run-with-xdist-installed"),
        pytest.param(None, False, False, id="plain-run-without-xdist"),
    ],
)
def test_only_the_xdist_controller_is_recognised_as_one(dist: str | None, worker: bool, expected: bool) -> None:
    assert suite_conftest._is_xdist_controller(_config(dist=dist, worker=worker)) is expected


@posix_only
@pytest.mark.parametrize(
    ("dist", "worker", "capped"),
    [
        pytest.param("load", False, False, id="controller-not-capped"),
        pytest.param("load", True, True, id="worker-capped"),
        pytest.param("no", False, True, id="plain-run-capped"),
    ],
)
def test_the_cap_is_applied_where_tests_run(
    monkeypatch: pytest.MonkeyPatch, dist: str, worker: bool, capped: bool
) -> None:
    import resource

    calls: list[tuple[int, tuple[int, int]]] = []
    monkeypatch.setattr(resource, "setrlimit", lambda which, limits: calls.append((which, limits)))
    suite_conftest._apply_address_space_cap(_config(dist=dist, worker=worker))
    if capped:
        assert calls and calls[0][0] == resource.RLIMIT_AS
        assert calls[0][1][0] == suite_conftest._MAX_RSS_BYTES
    else:
        assert calls == []


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        pytest.param(RuntimeError("can't start new thread"), True, id="thread"),
        pytest.param(BlockingIOError(11, "Resource temporarily unavailable"), True, id="fork"),
        pytest.param(MemoryError(), True, id="allocation"),
        pytest.param(RuntimeError("something else"), False, id="other-runtime-error"),
        pytest.param(ValueError("can't start new thread"), False, id="other-type"),
    ],
)
def test_resource_exhaustion_is_recognised(exc: BaseException, expected: bool) -> None:
    assert suite_conftest._is_resource_exhaustion(exc) is expected


def test_the_diagnostic_names_the_guard_and_its_override() -> None:
    message = suite_conftest._memory_guard_diagnostic()
    assert "RLIMIT_AS" in message
    assert "BERNSTEIN_MEM_GUARD_GB" in message
    assert "VmSize" in message


# ---------------------------------------------------------------------------
# Real sessions: the suite conftest loaded as a plugin into a throwaway tree.
# ---------------------------------------------------------------------------

_PROBE = """
import os
import resource

import pytest


@pytest.hookimpl(trylast=True)
def pytest_configure(config):
    role = "worker" if hasattr(config, "workerinput") else "controller"
    out = os.environ["RLIMIT_PROBE_DIR"]
    with open(os.path.join(out, f"{role}-{os.getpid()}.txt"), "w") as fh:
        fh.write(str(resource.getrlimit(resource.RLIMIT_AS)[0]))
"""


def _uncapped() -> None:
    """Start the child at its hard limit, not this process's guard.

    The suite running this test is itself capped by the same conftest, and a
    child inherits that cap. Without resetting it, "the controller kept the
    limit it started with" would hold whether or not the controller was capped.
    """
    import resource

    _soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    resource.setrlimit(resource.RLIMIT_AS, (hard, hard))


def _session(
    tmp_path: Path, test_body: str, *args: str, extra_path: str = "", probe: bool = False
) -> subprocess.CompletedProcess[str]:
    tree = tmp_path / "session"
    tree.mkdir()
    (tree / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tree / "rlimit_probe.py").write_text(_PROBE, encoding="utf-8")
    (tree / "test_probe.py").write_text(textwrap.dedent(test_body), encoding="utf-8")
    probe_dir = tmp_path / "limits"
    probe_dir.mkdir()
    env = dict(os.environ)
    env.pop("PYTEST_ADDOPTS", None)
    env["RLIMIT_PROBE_DIR"] = str(probe_dir)
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(tree), str(REPO_ROOT), str(REPO_ROOT / "src"), extra_path, env.get("PYTHONPATH", "")) if p
    )
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            str(tree / "pytest.ini"),
            "--rootdir",
            str(tree),
            "-p",
            "no:cacheprovider",
            "-p",
            "tests.conftest",
            *(["-p", "rlimit_probe"] if probe else []),
            *args,
            str(tree / "test_probe.py"),
        ],
        cwd=tree,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        preexec_fn=_uncapped if platform.system() != "Windows" else None,
    )


def _limits(tmp_path: Path, role: str) -> list[int]:
    return [int(p.read_text(encoding="utf-8")) for p in (tmp_path / "limits").glob(f"{role}-*.txt")]


@honours_rlimit_as
def test_memory_guard_not_applied_to_xdist_controller(tmp_path: Path) -> None:
    """With ``-n 2`` the controller keeps its limit and each worker gets the guard."""
    pytest.importorskip("xdist")
    import resource

    xdist_dir = str(Path(sys.modules["xdist"].__file__ or "").resolve().parents[1])
    hard = resource.getrlimit(resource.RLIMIT_AS)[1]
    proc = _session(
        tmp_path, "def test_a():\n    pass\n\ndef test_b():\n    pass\n", "-n", "2", extra_path=xdist_dir, probe=True
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    controller = _limits(tmp_path, "controller")
    workers = _limits(tmp_path, "worker")
    assert controller == [hard], (controller, hard)
    assert len(workers) == 2, workers
    assert all(limit == suite_conftest._memory_guard_bytes() for limit in workers), workers


@honours_rlimit_as
def test_a_plain_run_is_still_capped(tmp_path: Path) -> None:
    proc = _session(tmp_path, "def test_a():\n    pass\n", probe=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _limits(tmp_path, "controller") == [suite_conftest._memory_guard_bytes()]


def test_memory_guard_failure_is_named(tmp_path: Path) -> None:
    """A test that dies on ``can't start new thread`` is reported with the guard named."""
    proc = _session(
        tmp_path,
        """
        def test_spawns_a_thread():
            raise RuntimeError("can't start new thread")


        def test_unrelated_failure():
            raise RuntimeError("boom")
        """,
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "2 failed" in proc.stdout, proc.stdout + proc.stderr
    assert proc.stdout.count("memory guard: RLIMIT_AS soft limit is") == 1, proc.stdout
    assert "BERNSTEIN_MEM_GUARD_GB" in proc.stdout


def test_diagnostic_session_runs_without_resource_module(tmp_path: Path) -> None:
    """The diagnostic still runs on Windows, where the cap probe is unavailable."""
    shim = tmp_path / "windows_shim"
    shim.mkdir()
    (shim / "sitecustomize.py").write_text(
        "import builtins\nimport platform\n"
        "platform.system = lambda: 'Windows'\n"
        "original_import = builtins.__import__\n"
        "def without_resource(name, *args, **kwargs):\n"
        "    if name == 'resource':\n"
        "        raise ModuleNotFoundError('No module named resource')\n"
        "    return original_import(name, *args, **kwargs)\n"
        "builtins.__import__ = without_resource\n",
        encoding="utf-8",
    )
    proc = _session(
        tmp_path,
        'def test_thread_failure():\n    raise RuntimeError("can\'t start new thread")\n',
        extra_path=str(shim),
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "1 failed" in proc.stdout, proc.stdout + proc.stderr
    assert proc.stdout.count("memory guard: RLIMIT_AS soft limit is") == 1, proc.stdout


def test_child_session_preserves_configured_guard_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A documented custom cap reaches the child whose limit the probe measures."""
    monkeypatch.setenv("BERNSTEIN_MEM_GUARD_GB", "8")
    proc = _session(
        tmp_path,
        "import os\ndef test_override():\n    assert os.getenv('BERNSTEIN_MEM_GUARD_GB') == '8'\n",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "1 passed" in proc.stdout, proc.stdout + proc.stderr


def test_child_session_preserves_inherited_pythonpath(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A child can import modules supplied only on the invoking environment's path."""
    inherited = tmp_path / "inherited"
    inherited.mkdir()
    (inherited / "caller_module.py").write_text("VALUE = 42\n", encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(inherited))
    proc = _session(tmp_path, "import caller_module\ndef test_import():\n    assert caller_module.VALUE == 42\n")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "1 passed" in proc.stdout, proc.stdout + proc.stderr


@pytest.mark.parametrize("worker_crash", [True, False], ids=["worker-crash", "unrelated-error"])
def test_worker_crash_internalerror_names_guard(tmp_path: Path, worker_crash: bool) -> None:
    """A controller abort includes guard context without relabeling unrelated errors."""
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    body = (
        "def pytest_sessionstart(session):\n    crashitem = 'test_probe.py::test_a'\n    assert not crashitem\n"
        if worker_crash
        else "def pytest_sessionstart(session):\n    raise ValueError('unrelated controller error')\n"
    )
    (plugins / "controller_failure.py").write_text(body, encoding="utf-8")
    proc = _session(
        tmp_path,
        "def test_a():\n    pass\n",
        "-p",
        "controller_failure",
        extra_path=str(plugins),
    )
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert "INTERNALERROR" in proc.stdout + proc.stderr
    assert ((proc.stdout + proc.stderr).count("memory guard: RLIMIT_AS soft limit is") == 1) is worker_crash
