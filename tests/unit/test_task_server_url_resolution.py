"""The run's own port must reach the agent, not the historical 8052 default.

Regression for the 2026-09-02 smoke: `bernstein run --port N` never published
its port, so the prompt's auth section told the agent to POST to 8052 - another
run's server - which answered 401 and got the task failed as auth_error.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bernstein.core.agents.spawner_core import _render_auth_section, _resolve_task_server_url
from bernstein.core.defaults import SDD_SERVER_PORT


@pytest.fixture(autouse=True)
def _no_ambient_server_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BERNSTEIN_SERVER_URL", raising=False)


def _write_port(workdir: Path, port: int) -> None:
    path = workdir / SDD_SERVER_PORT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{port}\n", encoding="utf-8")


def test_env_var_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BERNSTEIN_SERVER_URL", "http://10.0.0.5:9000/")
    _write_port(tmp_path, 60479)
    assert _resolve_task_server_url(tmp_path) == "http://10.0.0.5:9000"


def test_falls_back_to_the_run_port_file(tmp_path: Path) -> None:
    _write_port(tmp_path, 60479)
    assert _resolve_task_server_url(tmp_path) == "http://127.0.0.1:60479"


def test_falls_back_to_8052_without_a_port_file(tmp_path: Path) -> None:
    assert _resolve_task_server_url(tmp_path) == "http://127.0.0.1:8052"


@pytest.mark.parametrize("bad", ["not-a-port", "0", "70000"])
def test_rejects_an_unusable_port_file(tmp_path: Path, bad: str) -> None:
    path = tmp_path / SDD_SERVER_PORT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(bad, encoding="utf-8")
    assert _resolve_task_server_url(tmp_path) == "http://127.0.0.1:8052"


def test_no_workdir_keeps_the_historical_default(tmp_path: Path) -> None:
    assert _resolve_task_server_url() == "http://127.0.0.1:8052"


def test_auth_section_carries_the_run_port(tmp_path: Path) -> None:
    """The prompt block the agent actually reads must name the run's port."""
    _write_port(tmp_path, 60479)
    section = _render_auth_section(tmp_path / "token.json", tmp_path)
    assert "127.0.0.1:60479" in section
    assert "127.0.0.1:8052" not in section
