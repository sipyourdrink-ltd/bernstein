"""Unit tests for MuseAdapter (Muse Code, ``muse exec`` headless mode)."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters.env_isolation import build_filtered_env
from bernstein.adapters.muse import DEFAULT_MODEL, MuseAdapter
from tests.unit._adapter_test_helpers import inner_cmd, make_popen_mock

if TYPE_CHECKING:
    from pathlib import Path


pytestmark = pytest.mark.usefixtures("no_watchdog_threads")


def _spawn(adapter: MuseAdapter, tmp_path: Path, *, model: str = DEFAULT_MODEL, prompt: str = "fix the bug") -> object:
    return adapter.spawn(
        prompt=prompt,
        workdir=tmp_path,
        model_config=ModelConfig(model=model, effort="high"),
        session_id="muse-s1",
    )


# ---------------------------------------------------------------------------
# Spawn command construction
# ---------------------------------------------------------------------------


def test_spawn_builds_exec_command(tmp_path: Path) -> None:
    adapter = MuseAdapter()
    proc_mock = make_popen_mock(800)

    with patch("bernstein.adapters.muse.subprocess.Popen", return_value=proc_mock) as popen:
        _spawn(adapter, tmp_path)

    inner = inner_cmd(popen.call_args.args[0])
    assert inner == [
        "muse",
        "--model",
        "muse-spark-1.2",
        "--disable-approval",
        "exec",
        "fix the bug",
    ]


def test_spawn_empty_model_uses_documented_default(tmp_path: Path) -> None:
    adapter = MuseAdapter()
    proc_mock = make_popen_mock(801)

    with patch("bernstein.adapters.muse.subprocess.Popen", return_value=proc_mock) as popen:
        _spawn(adapter, tmp_path, model="")

    inner = inner_cmd(popen.call_args.args[0])
    assert inner[1:3] == ["--model", DEFAULT_MODEL]


def test_spawn_passes_explicit_vendor_model_id_through(tmp_path: Path) -> None:
    adapter = MuseAdapter()
    proc_mock = make_popen_mock(802)

    with patch("bernstein.adapters.muse.subprocess.Popen", return_value=proc_mock) as popen:
        _spawn(adapter, tmp_path, model="muse-spark-2.0-preview")

    inner = inner_cmd(popen.call_args.args[0])
    assert inner[1:3] == ["--model", "muse-spark-2.0-preview"]


@pytest.mark.parametrize("tier", ["sonnet", "opus", "haiku", "Sonnet"])
def test_spawn_maps_claude_tier_names_to_the_vendor_default(tmp_path: Path, tier: str) -> None:
    """The shared selectors emit Claude tier names (the ``sonnet`` default
    reaches every adapter); Muse cannot run them, so they map onto the
    vendor default instead of failing before Popen - the same safety net
    the Codex and Copilot adapters use."""
    adapter = MuseAdapter()
    proc_mock = make_popen_mock(803)

    with patch("bernstein.adapters.muse.subprocess.Popen", return_value=proc_mock) as popen:
        _spawn(adapter, tmp_path, model=tier)

    inner = inner_cmd(popen.call_args.args[0])
    assert inner[1:3] == ["--model", DEFAULT_MODEL]


def test_spawn_unknown_logical_model_fails_loudly(tmp_path: Path) -> None:
    """Single-model vendor lineup: never silently remap a foreign name."""
    adapter = MuseAdapter()

    with (
        patch("bernstein.adapters.muse.subprocess.Popen") as popen,
        pytest.raises(ValueError, match="gpt-6-nano"),
    ):
        _spawn(adapter, tmp_path, model="gpt-6-nano")

    popen.assert_not_called()


def test_spawn_appends_system_addendum_to_the_prompt(tmp_path: Path) -> None:
    """Muse has no separate system-prompt channel; a non-empty addendum
    must reach the agent by riding on the exec prompt (base contract
    fallback), or completion/signal/heartbeat instructions are lost."""
    adapter = MuseAdapter()
    proc_mock = make_popen_mock(804)

    with patch("bernstein.adapters.muse.subprocess.Popen", return_value=proc_mock) as popen:
        adapter.spawn(
            prompt="fix the bug",
            workdir=tmp_path,
            model_config=ModelConfig(model=DEFAULT_MODEL, effort="high"),
            session_id="muse-s1",
            system_addendum="When done, POST /complete.",
        )

    inner = inner_cmd(popen.call_args.args[0])
    assert inner[:5] == ["muse", "--model", DEFAULT_MODEL, "--disable-approval", "exec"]
    assert inner[5] == "fix the bug\n\nWhen done, POST /complete."


def test_spawn_empty_addendum_leaves_prompt_untouched(tmp_path: Path) -> None:
    adapter = MuseAdapter()
    proc_mock = make_popen_mock(805)

    with patch("bernstein.adapters.muse.subprocess.Popen", return_value=proc_mock) as popen:
        _spawn(adapter, tmp_path, prompt="just the task")

    inner = inner_cmd(popen.call_args.args[0])
    assert inner[-1] == "just the task"


# ---------------------------------------------------------------------------
# Env isolation
# ---------------------------------------------------------------------------


def test_meta_api_key_survives_env_filtering(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("META_API_KEY", "sk-meta-test")
    monkeypatch.setenv("SOME_UNRELATED_SECRET", "leak-me-not")
    env = build_filtered_env(["META_API_KEY"])
    assert env["META_API_KEY"] == "sk-meta-test"
    assert "SOME_UNRELATED_SECRET" not in env


def test_spawn_env_contains_meta_api_key_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("META_API_KEY", "sk-meta-test")
    monkeypatch.setenv("DATABASE_URL", "postgres://secret")
    adapter = MuseAdapter()
    proc_mock = make_popen_mock(803)

    with patch("bernstein.adapters.muse.subprocess.Popen", return_value=proc_mock) as popen:
        _spawn(adapter, tmp_path)

    env = popen.call_args.kwargs["env"]
    assert env["META_API_KEY"] == "sk-meta-test"
    assert "DATABASE_URL" not in env


# ---------------------------------------------------------------------------
# Missing binary / version probe / name
# ---------------------------------------------------------------------------


def test_spawn_translates_missing_cli(tmp_path: Path) -> None:
    adapter = MuseAdapter()
    with (
        patch(
            "bernstein.adapters.muse.subprocess.Popen",
            side_effect=FileNotFoundError("No such file"),
        ),
        pytest.raises(RuntimeError) as excinfo,
    ):
        _spawn(adapter, tmp_path)

    message = str(excinfo.value)
    assert "muse not found" in message
    assert "https://dev.meta.ai/install.sh" in message


def test_get_version_returns_probe_output() -> None:
    adapter = MuseAdapter()
    with patch("bernstein.adapters.muse.subprocess.run") as run:
        run.return_value.returncode = 0
        run.return_value.stdout = "muse 1.4.0\n"
        assert adapter.get_version() == "muse 1.4.0"
    assert run.call_args.args[0] == ["muse", "--version"]


def test_get_version_returns_none_when_probe_fails() -> None:
    adapter = MuseAdapter()
    with patch("bernstein.adapters.muse.subprocess.run", side_effect=OSError("boom")):
        assert adapter.get_version() is None


def test_name() -> None:
    assert MuseAdapter().name() == "muse"


def test_registered_in_registry() -> None:
    from bernstein.adapters.registry import get_adapter

    assert isinstance(get_adapter("muse"), MuseAdapter)
