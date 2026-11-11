"""Real test-owned pools must be joined even when their test body fails."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import TypedDict

REPO_ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR_TESTS = REPO_ROOT / "tests/unit/test_orchestrator.py"


class PoolWitness(TypedDict):
    pool: int
    workers_alive: list[bool]


def _run_resource_probe(
    tmp_path: Path, body: str, selection: str
) -> tuple[subprocess.CompletedProcess[str], list[PoolWitness]]:
    """Run the actual module's factories/fixtures in a separate native pytest."""
    probe = tmp_path / "test_orchestrator.py"
    probe.write_text(
        ORCHESTRATOR_TESTS.read_text()
        + textwrap.dedent(
            """

            _resource_probe_pools = []
            _resource_probe_original_build = _build_orchestrator

            def _build_orchestrator(*args, **kwargs):
                orch = _resource_probe_original_build(*args, **kwargs)
                _resource_probe_pools.append(orch._executor)
                return orch
            """
        )
        + textwrap.dedent(body)
    )
    witness = tmp_path / "resource-witness.json"
    (tmp_path / "conftest.py").write_text(
        textwrap.dedent(
            """
            import json
            from pathlib import Path

            def pytest_sessionfinish(session, exitstatus):
                module = session.items[0].module
                # Inspect only the exact pools the probe deliberately owns.
                rows = [
                    {"pool": id(pool), "workers_alive": [t.is_alive() for t in pool._threads]}
                    for pool in module._resource_probe_pools
                ]
                Path(__file__).with_name("resource-witness.json").write_text(json.dumps(rows))
            """
        )
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join((str(REPO_ROOT / "src"), str(REPO_ROOT)))
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            str(REPO_ROOT / "pyproject.toml"),
            "-p",
            "tests.conftest",
            "-p",
            "tests.unit.conftest",
            "--basetemp",
            str(tmp_path / "native-tmp"),
            str(probe),
            "-k",
            selection,
            "-q",
            "--tb=short",
            "-p",
            "no:cacheprovider",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=45,
    )
    (tmp_path / "native.stdout.log").write_text(result.stdout)
    (tmp_path / "native.stderr.log").write_text(result.stderr)
    (tmp_path / "native.exit.json").write_text(json.dumps({"returncode": result.returncode, "argv": result.args}))
    assert witness.exists(), result.stdout + result.stderr
    return result, json.loads(witness.read_text())


def _assert_owned_workers_joined(rows: list[PoolWitness], count: int) -> None:
    assert len(rows) == count
    assert len({row["pool"] for row in rows}) == count
    assert all(row["workers_alive"] for row in rows), "Each probe must execute real pool work"
    assert all(not alive for row in rows for alive in row["workers_alive"]), rows


def test_every_test_owned_pool_is_joined_after_multiple_constructions(tmp_path: Path) -> None:
    result, rows = _run_resource_probe(
        tmp_path,
        """
        def test_resource_probe_multiple(tmp_path):
            for index in range(2):
                workdir = tmp_path / str(index)
                workdir.mkdir()
                orch = _build_orchestrator(workdir, _mock_transport({}))
                orch._executor.submit(lambda: None).result()
            workdir = tmp_path / "direct"
            workdir.mkdir()
            orch = TestCheckEvolve()._make_orch(workdir)
            _resource_probe_pools.append(orch._executor)
            orch._executor.submit(lambda: None).result()
        """,
        "resource_probe_multiple",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    _assert_owned_workers_joined(rows, 3)


def test_failed_test_body_still_joins_its_owned_pool(tmp_path: Path) -> None:
    result, rows = _run_resource_probe(
        tmp_path,
        """
        def test_resource_probe_failure(tmp_path):
            orch = _build_orchestrator(tmp_path, _mock_transport({}))
            orch._executor.submit(lambda: None).result()
            assert False, "original-resource-probe-failure"
        """,
        "resource_probe_failure",
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "original-resource-probe-failure" in result.stdout
    assert "1 failed" in result.stdout
    assert "ERROR" not in result.stdout
    _assert_owned_workers_joined(rows, 1)


def test_parallel_verification_pools_are_joined_after_real_overlap(tmp_path: Path) -> None:
    result, rows = _run_resource_probe(
        tmp_path,
        "",
        "TestParallelVerification or TestProcessCompletedTasksParallel",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed" in result.stdout
    _assert_owned_workers_joined(rows, 2)


def test_constructor_failure_still_joins_the_pool_it_created(tmp_path: Path) -> None:
    result, rows = _run_resource_probe(
        tmp_path,
        """
        def test_resource_probe_constructor_failure(tmp_path):
            # Invoke the real initializer on a retained instance so the witness
            # can inspect its exact pool after the original exception propagates.
            orch = Orchestrator.__new__(Orchestrator)
            client = httpx.Client(transport=_mock_transport({}))
            spawner = AgentSpawner(_mock_adapter(), tmp_path / "templates", tmp_path)
            try:
                with patch("bernstein.core.orchestration.orchestrator.FastPathStats",
                           side_effect=ValueError("original-constructor-probe-failure")):
                    with pytest.raises(ValueError, match="original-constructor-probe-failure"):
                        Orchestrator.__init__(orch, OrchestratorConfig(), spawner, tmp_path, client=client)
                _resource_probe_pools.append(orch._executor)
                orch._executor.submit(lambda: None).result()
                assert not client.is_closed  # Passed-in resources remain borrowed.
            finally:
                client.close()  # The probe, not the fixture, owns this client.
        """,
        "resource_probe_constructor_failure",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    _assert_owned_workers_joined(rows, 1)
