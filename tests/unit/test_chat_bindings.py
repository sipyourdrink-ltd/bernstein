"""Unit tests for the chat-thread binding store."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.chat.bindings import Binding, BindingStore, _atomic_write


def test_binding_round_trip(tmp_path: Path) -> None:
    """put/get/delete should survive a fresh store instance."""
    store = BindingStore(tmp_path)
    binding = Binding(
        platform="telegram",
        thread_id="42",
        session_id="sess-a",
        task_id="t-1",
        adapter="claude",
        goal="Add JWT auth",
        status_message_id="100",
    )
    store.put(binding)

    # New instance reads from disk.
    reopened = BindingStore(tmp_path)
    loaded = reopened.get("telegram", "42")
    assert loaded is not None
    assert loaded.session_id == "sess-a"
    assert loaded.goal == "Add JWT auth"

    assert reopened.delete("telegram", "42") is True
    assert reopened.get("telegram", "42") is None


def test_atomic_write_leaves_no_tmp_files_on_success(tmp_path: Path) -> None:
    """``_atomic_write`` should produce a single final file."""
    target = tmp_path / "bindings.json"
    _atomic_write(target, json.dumps({"hello": "world"}))

    assert target.exists()
    assert json.loads(target.read_text()) == {"hello": "world"}
    siblings = list(tmp_path.iterdir())
    assert siblings == [target], f"unexpected residue: {siblings}"


def test_atomic_write_replaces_existing_file(tmp_path: Path) -> None:
    """Second write must fully replace the first."""
    target = tmp_path / "bindings.json"
    _atomic_write(target, '{"v": 1}')
    _atomic_write(target, '{"v": 2}')
    assert json.loads(target.read_text()) == {"v": 2}


def test_corrupt_file_does_not_brick_store(tmp_path: Path) -> None:
    """Unparseable JSON on disk should be treated as an empty store."""
    chat_dir = tmp_path / ".sdd" / "chat"
    chat_dir.mkdir(parents=True)
    (chat_dir / "bindings.json").write_text("{not-json")

    store = BindingStore(tmp_path)
    assert store.all() == []
    store.put(Binding(platform="telegram", thread_id="7"))
    assert store.get("telegram", "7") is not None


def test_all_returns_snapshot(tmp_path: Path) -> None:
    """``all()`` should return a list independent of internal cache."""
    store = BindingStore(tmp_path)
    store.put(Binding(platform="telegram", thread_id="1"))
    store.put(Binding(platform="discord", thread_id="2"))

    snapshot = store.all()
    assert {b.platform for b in snapshot} == {"telegram", "discord"}

    # Mutating the snapshot must not affect the store.
    snapshot.clear()
    assert len(store.all()) == 2


def test_binding_key_composition() -> None:
    """The storage key should concatenate platform and thread id."""
    binding = Binding(platform="slack", thread_id="C0XYZ")
    assert binding.key == "slack:C0XYZ"


@pytest.mark.parametrize("platform", ["telegram", "discord", "slack"])
def test_store_separates_per_platform(tmp_path: Path, platform: str) -> None:
    """Bindings for different platforms must not collide."""
    store = BindingStore(tmp_path)
    store.put(Binding(platform=platform, thread_id="shared"))
    other = "discord" if platform != "discord" else "slack"
    assert store.get(other, "shared") is None
    assert store.get(platform, "shared") is not None
