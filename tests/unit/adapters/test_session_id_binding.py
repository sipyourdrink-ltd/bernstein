"""Tests for deterministic session-id binding (replay isolation).

Covers:

* :func:`bernstein.adapters.session_id.derive_session_id` derivation and the
  property that it is stable across runs and distinct across inputs (AC #1,
  #5).
* The ``session_id_flag`` contract field (AC #2).
* Spawn-time wiring via ``CLIAdapter.session_id_args`` and the codex adapter
  (AC #3, #6).
* The replay index that locates a prior run by ``(conversation_id,
  adapter_name)`` without scanning logs (AC #4).
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters import _contract
from bernstein.adapters.base import CLIAdapter
from bernstein.adapters.codex import CodexAdapter
from bernstein.adapters.session_id import (
    DERIVE_RECIPE_VERSION,
    SESSION_INDEX_FILENAME,
    SessionIdIndex,
    derive_session_id,
)
from tests.unit._adapter_test_helpers import inner_cmd

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.usefixtures("no_watchdog_threads")


# ---------------------------------------------------------------------------
# AC #1 / #5 - derive_session_id
# ---------------------------------------------------------------------------


def test_derive_returns_uuid() -> None:
    result = derive_session_id("conv-1", "codex")
    assert isinstance(result, UUID)


def test_derive_is_deterministic_across_calls() -> None:
    # Stable across calls (and, since the recipe is pure + stdlib-only,
    # across processes and runs).
    a = derive_session_id("conv-1", "codex")
    b = derive_session_id("conv-1", "codex")
    assert a == b


def test_derive_differs_across_conversation_id() -> None:
    a = derive_session_id("conv-1", "codex")
    b = derive_session_id("conv-2", "codex")
    assert a != b


def test_derive_differs_across_adapter_name() -> None:
    a = derive_session_id("conv-1", "codex")
    b = derive_session_id("conv-1", "claude")
    assert a != b


def test_derive_is_version_5_shaped() -> None:
    result = derive_session_id("conv-1", "codex")
    assert result.version == 5
    # RFC 4122 variant bits.
    assert (result.bytes[8] & 0xC0) == 0x80


@pytest.mark.parametrize(
    "conversation_id,adapter_name",
    [("", "codex"), ("conv", ""), ("", "")],
)
def test_derive_rejects_empty_inputs(conversation_id: str, adapter_name: str) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        derive_session_id(conversation_id, adapter_name)


def test_derive_rejects_non_string() -> None:
    with pytest.raises(TypeError):
        derive_session_id(123, "codex")  # type: ignore[arg-type]


def test_derive_property_grid() -> None:
    # Distinct (conversation, adapter) pairs map to distinct ids; equal
    # pairs map to one id (AC #5 property formalised as a small grid).
    conversations = ["c1", "c2", "c3"]
    adapters = ["codex", "claude", "gemini"]
    seen: dict[UUID, tuple[str, str]] = {}
    for conv in conversations:
        for adapter in adapters:
            uid = derive_session_id(conv, adapter)
            assert uid == derive_session_id(conv, adapter)
            assert uid not in seen, f"collision: {(conv, adapter)} vs {seen.get(uid)}"
            seen[uid] = (conv, adapter)
    assert len(seen) == len(conversations) * len(adapters)


# ---------------------------------------------------------------------------
# AC #2 - contract field
# ---------------------------------------------------------------------------


def test_contract_session_id_flag_defaults_to_none() -> None:
    spec = _contract.ContractSpec(
        adapter="x",
        binary="x",
        install_method="",
        install_spec="",
        auth_required_for_help=False,
        auth_required_for_models=False,
        auth_secret_env="",
        required_flags=(),
        required_subcommands=(),
        help_command=(),
        models_command=(),
        models_required_present=(),
    )
    assert spec.session_id_flag is None


def test_contract_loads_session_id_flag_from_yaml(tmp_path: Path) -> None:
    (tmp_path / "demo.yaml").write_text(
        "adapter: demo\nbinary: demo\nsession_id_flag: '--session-id'\n",
        encoding="utf-8",
    )
    spec = _contract.ContractSpec.load("demo", contracts_dir=tmp_path)
    assert spec.session_id_flag == "--session-id"


def test_contract_blank_session_id_flag_is_none(tmp_path: Path) -> None:
    (tmp_path / "demo.yaml").write_text(
        "adapter: demo\nbinary: demo\nsession_id_flag: ''\n",
        encoding="utf-8",
    )
    spec = _contract.ContractSpec.load("demo", contracts_dir=tmp_path)
    assert spec.session_id_flag is None


def test_real_codex_contract_declares_no_session_id_flag() -> None:
    # Property (1), root cause. ``codex exec`` has no top-level flag that
    # accepts a caller-supplied session id - the only session surface is the
    # ``resume <SESSION_ID>`` subcommand, which reattaches to a session that
    # already exists and so cannot bind one at spawn time. A contract that
    # names a flag here makes ``session_id_args`` emit it into every spawn
    # (issue #4135).
    spec = _contract.ContractSpec.load("codex")
    assert spec.session_id_flag is None


def test_real_copilot_contract_declares_session_id_flag() -> None:
    spec = _contract.ContractSpec.load("copilot")
    assert spec.session_id_flag == "--session-id"


# ---------------------------------------------------------------------------
# AC #3 - spawn-time wiring
# ---------------------------------------------------------------------------


class _FlaglessAdapter(CLIAdapter):
    """Adapter whose namespace has no contract on disk."""

    registry_name = "no-such-adapter-xyz"

    def spawn(self, **_kwargs: object) -> object:  # pragma: no cover - unused
        raise NotImplementedError

    def name(self) -> str:
        return "Flagless"


def test_session_id_args_empty_when_no_contract() -> None:
    adapter = _FlaglessAdapter()
    assert adapter.session_id_args("conv-1") == []


def test_session_id_args_empty_when_flag_absent(tmp_path: Path) -> None:
    (tmp_path / "demo.yaml").write_text("adapter: demo\nbinary: demo\n", encoding="utf-8")
    spec = _contract.ContractSpec.load("demo", contracts_dir=tmp_path)
    with patch.object(_contract.ContractSpec, "load", return_value=spec):
        adapter = _FlaglessAdapter()
        assert adapter.session_id_args("conv-1") == []


def test_codex_session_id_args_is_empty() -> None:
    # Property (1). No declared flag means no argv contribution; the derived
    # id is still recorded in orchestrator state for cross-reference.
    adapter = CodexAdapter()
    assert adapter.session_id_args("conv-1") == []


def test_codex_declares_no_native_resume() -> None:
    # Property (3). ``codex exec resume <SESSION_ID>`` is a subcommand, not a
    # flag, and reattaching is not wired for this adapter: the contract
    # declares resume unsupported, so no resume token is built at all.
    from bernstein.adapters._contract import ResumeStrategy

    assert CodexAdapter().strategy().resume is ResumeStrategy.UNSUPPORTED


def test_copilot_session_id_args_unchanged_by_codex_fix() -> None:
    # Property (2), blast radius. ``session_id_args`` is shared: copilot must
    # keep pinning its derived id through the flag its own CLI does accept.
    from bernstein.adapters.copilot import CopilotAdapter

    adapter = CopilotAdapter()
    assert adapter.session_id_args("conv-1") == [
        "--session-id",
        str(derive_session_id("conv-1", "copilot")),
    ]


def test_codex_spawn_argv_has_no_session_id_flag(tmp_path: Path) -> None:
    # Property (1) at the spawn boundary. This is the exact argv codex-cli
    # rejected with ``error: unexpected argument '--session-id' found``.
    adapter = CodexAdapter()
    proc = MagicMock(spec=subprocess.Popen)
    proc.pid = 4242
    proc.stdout = MagicMock()

    with patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc) as popen:
        adapter.spawn(
            prompt="do work",
            workdir=tmp_path,
            model_config=ModelConfig(model="gpt-5.5", effort="low"),
            session_id="qa-abc123",
            timeout_seconds=0,
        )

    argv = list(popen.call_args_list[0][0][0])
    assert "--session-id" not in argv
    assert str(derive_session_id("qa-abc123", "codex")) not in argv
    # The prompt stays last so codex reads it as the positional PROMPT.
    assert argv[-1] == "do work"


def test_copilot_spawn_argv_still_pins_session_id(tmp_path: Path) -> None:
    # Property (2) at the spawn boundary: the shared helper still emits
    # copilot's flag/id pair after the codex contract change.
    from bernstein.adapters.copilot import CopilotAdapter

    adapter = CopilotAdapter()
    proc = MagicMock(spec=subprocess.Popen)
    proc.pid = 4343
    proc.stdout = MagicMock()

    with patch("bernstein.adapters.copilot.subprocess.Popen", return_value=proc) as popen:
        adapter.spawn(
            prompt="do work",
            workdir=tmp_path,
            model_config=ModelConfig(model="gpt-5.4", effort="high"),
            session_id="copilot-4135",
            timeout_seconds=0,
        )

    inner = inner_cmd(popen.call_args.args[0])
    assert inner[inner.index("--session-id") + 1] == str(derive_session_id("copilot-4135", "copilot"))


# ---------------------------------------------------------------------------
# AC #4 - replay index lookup without scanning logs
# ---------------------------------------------------------------------------


def test_index_record_and_lookup_roundtrip(tmp_path: Path) -> None:
    index = SessionIdIndex(tmp_path)
    record = index.record("conv-1", "codex", "run-A")
    assert record.session_id == str(derive_session_id("conv-1", "codex"))
    assert record.run_id == "run-A"
    assert record.recipe_version == DERIVE_RECIPE_VERSION

    found = index.lookup("conv-1", "codex")
    assert found is not None
    assert found.run_id == "run-A"
    assert found.session_id == record.session_id


def test_index_lookup_misses_return_none(tmp_path: Path) -> None:
    index = SessionIdIndex(tmp_path)
    index.record("conv-1", "codex", "run-A")
    assert index.lookup("conv-2", "codex") is None
    assert index.lookup("conv-1", "claude") is None


def test_index_rerun_overwrites_slot(tmp_path: Path) -> None:
    index = SessionIdIndex(tmp_path)
    index.record("conv-1", "codex", "run-A")
    index.record("conv-1", "codex", "run-B")
    found = index.lookup("conv-1", "codex")
    assert found is not None
    assert found.run_id == "run-B"


def test_index_persists_to_named_file(tmp_path: Path) -> None:
    index = SessionIdIndex(tmp_path)
    index.record("conv-1", "codex", "run-A")
    assert (tmp_path / SESSION_INDEX_FILENAME).exists()
    # A fresh handle reads the same data back (cross-process durability).
    assert SessionIdIndex(tmp_path).lookup("conv-1", "codex") is not None


def test_index_tolerates_corrupt_file(tmp_path: Path) -> None:
    (tmp_path / SESSION_INDEX_FILENAME).write_text("{ not json", encoding="utf-8")
    index = SessionIdIndex(tmp_path)
    assert index.lookup("conv-1", "codex") is None
    # Recording recovers cleanly over the corrupt file.
    index.record("conv-1", "codex", "run-A")
    assert index.lookup("conv-1", "codex") is not None


def test_replay_package_locate_run_resolves_index(tmp_path: Path) -> None:
    from bernstein.core.replay import locate_run, record_run

    record_run(tmp_path, "conv-1", "codex", "run-A")
    found = locate_run(tmp_path, "conv-1", "codex")
    assert found is not None
    assert found.run_id == "run-A"
    assert locate_run(tmp_path, "conv-9", "codex") is None
