"""Tests for the bounded OCI process boundary using a test-only runtime."""

from __future__ import annotations

import subprocess

import pytest

from bernstein.core.sandbox import isolated_process


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    executable = tmp_path / "docker"
    executable.write_text(
        f"""#!/bin/sh
if [ "$1" = rm ]; then
    printf '%s\\n' "$3" >> '{tmp_path}/removed'
    exit 0
fi
printf '%s\\n' "$@" > '{tmp_path}/argv'
while [ "$1" != -- ]; do shift; done
shift 2
exec "$@"
"""
    )
    executable.chmod(0o755)
    monkeypatch.setattr(isolated_process, "runtime_executable", lambda: str(executable))
    return tmp_path


def test_runner_passes_only_explicit_mounts_and_bounds(runtime):
    result = isolated_process.run_isolated(
        "fixture-image",
        ["cat"],
        input_data=b"fixture",
        timeout=3,
    )
    assert result.returncode == 0
    assert result.stdout == b"fixture"
    args = (runtime / "argv").read_text().splitlines()
    assert "--network=none" in args
    assert "--read-only" in args
    assert "--cap-drop=ALL" in args
    assert "--security-opt=no-new-privileges" in args
    assert "--pids-limit=32" in args
    assert "--mount" not in args and "--env" not in args
    assert args[-3:] == ["--", "fixture-image", "cat"]
    assert (runtime / "removed").read_text().strip() == args[args.index("--name") + 1]


def test_runner_stops_after_bounded_output(runtime):
    with pytest.raises(isolated_process.IsolationUnavailable, match="output limit"):
        isolated_process.run_isolated(
            "fixture-image",
            ["head", "-c", "70000", "/dev/zero"],
            input_data=b"",
            timeout=3,
        )
    assert (runtime / "removed").read_text().startswith("bernstein-isolated-")


def test_runner_removes_timed_out_container(runtime):
    with pytest.raises(subprocess.TimeoutExpired):
        isolated_process.run_isolated("fixture-image", ["sleep", "5"], input_data=b"", timeout=0.1)
    assert (runtime / "removed").read_text().startswith("bernstein-isolated-")


def test_readonly_bind_rejects_mount_separators(tmp_path):
    with pytest.raises(isolated_process.IsolationUnavailable):
        isolated_process.readonly_bind(tmp_path / "path,with-comma", "/workspace")
