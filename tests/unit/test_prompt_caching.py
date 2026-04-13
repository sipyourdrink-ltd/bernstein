"""Tests for prompt caching orchestration (501a)."""

from __future__ import annotations

import json
import tempfile
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import Mock

import pytest
from bernstein.core.models import ModelConfig
from bernstein.core.prompt_caching import (
    MAX_AGENT_CACHE_ENTRIES,
    AgentCacheTracker,
    CacheBreakEvent,
    CacheBreakReason,
    CacheEntry,
    CacheManifest,
    CacheSafeParams,
    PromptCachingManager,
    _estimate_tokens,
    build_cache_safe_fork_params,
    compute_cache_key,
    extract_system_prefix,
    make_prompt_cache_key,
)

from bernstein.adapters.base import CLIAdapter, SpawnResult
from bernstein.adapters.caching_adapter import CachingAdapter

# ---------------------------------------------------------------------------
# CacheEntry / CacheManifest
# ---------------------------------------------------------------------------


def test_cache_entry_creation() -> None:
    """Cache entry stores prefix hash and metadata."""
    entry = CacheEntry(
        cache_key="abc123def456",
        system_prefix="You are a backend engineer.",
        prefix_tokens=42,
        hit_count=0,
        first_seen_at=1234567890.0,
    )
    assert entry.cache_key == "abc123def456"
    assert entry.system_prefix == "You are a backend engineer."
    assert entry.prefix_tokens == 42
    assert entry.hit_count == 0


def test_cache_manifest_serialization() -> None:
    """Manifest can be serialized to JSON-lines format."""
    manifest = CacheManifest(entries={})
    entry = CacheEntry(
        cache_key="abc123",
        system_prefix="test",
        prefix_tokens=10,
        hit_count=0,
        first_seen_at=1234567890.0,
    )
    manifest.entries["abc123"] = entry

    json_line = manifest.to_json_line()
    assert "abc123" in json_line
    assert "test" in json_line


def test_cache_manifest_round_trip() -> None:
    """Manifest serializes and deserializes without data loss."""
    manifest = CacheManifest(total_cached_requests=5)
    manifest.entries["key1"] = CacheEntry(
        cache_key="key1",
        system_prefix="You are a qa engineer.",
        prefix_tokens=15,
        hit_count=3,
        first_seen_at=1000.0,
        last_used_at=2000.0,
    )
    line = manifest.to_json_line()
    restored = CacheManifest.from_json_line(line)

    assert restored.total_cached_requests == 5
    assert "key1" in restored.entries
    assert restored.entries["key1"].hit_count == 3
    assert restored.entries["key1"].last_used_at == pytest.approx(2000.0)


# ---------------------------------------------------------------------------
# extract_system_prefix / compute_cache_key
# ---------------------------------------------------------------------------


def test_extract_system_prefix_simple() -> None:
    """Extract role prompt as system prefix when no other sections."""
    prompt = "You are a backend engineer.\n\n## Assigned tasks\nTask 1: foo"
    prefix, suffix = extract_system_prefix(prompt)
    assert "You are a backend engineer." in prefix
    assert "## Assigned tasks" not in prefix
    assert "## Assigned tasks" in suffix


def test_extract_system_prefix_with_specialist() -> None:
    """Extract role + specialist block as prefix."""
    prompt = (
        "You are a backend engineer.\n\n"
        "## Available specialist agents\n"
        "- Agent A: desc\n\n"
        "## Project context\n"
        "Project info\n\n"
        "## Assigned tasks\n"
        "Task 1: foo"
    )
    prefix, suffix = extract_system_prefix(prompt)
    assert "You are a backend engineer." in prefix
    assert "Available specialist agents" in prefix
    assert "Project context" in prefix
    assert "## Assigned tasks" not in prefix
    assert "Task 1" in suffix


def test_extract_system_prefix_handles_missing_sections() -> None:
    """Prefix extraction works even if some sections are missing."""
    prompt = "You are a backend engineer.\n\n## Assigned tasks\nTask 1"
    prefix, suffix = extract_system_prefix(prompt)
    assert len(prefix) > 0
    assert len(suffix) > 0
    assert "backend" in prefix.lower()


def test_extract_system_prefix_no_marker() -> None:
    """When no task/instruction marker present, entire prompt is prefix."""
    prompt = "You are a backend engineer."
    prefix, suffix = extract_system_prefix(prompt)
    assert prefix == prompt
    assert suffix == ""


def test_compute_cache_key() -> None:
    """Cache key is SHA-256 hash of prefix."""
    prefix = "test system prompt"
    key = compute_cache_key(prefix)
    assert isinstance(key, str)
    assert len(key) == 64  # SHA-256 hex is 64 chars
    key2 = compute_cache_key(prefix)
    assert key == key2


def test_compute_cache_key_differs_for_different_prefixes() -> None:
    """Different prefixes produce different keys."""
    k1 = compute_cache_key("You are a backend engineer.")
    k2 = compute_cache_key("You are a qa engineer.")
    assert k1 != k2


# ---------------------------------------------------------------------------
# PromptCachingManager
# ---------------------------------------------------------------------------


def test_caching_manager_tracks_prefix() -> None:
    """Manager records first sighting of a new prefix."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)
        mgr = PromptCachingManager(workdir)

        prompt = "System prompt\n\n## Assigned tasks\nTask 1"
        result = mgr.process_prompt(prompt)

        assert result.cache_key is not None
        assert "System prompt" in result.system_prefix
        assert "## Assigned tasks" in result.task_suffix
        assert result.is_new_prefix is True


def test_caching_manager_detects_reuse() -> None:
    """Manager detects when same prefix is reused."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)
        mgr = PromptCachingManager(workdir)

        prompt1 = "System prompt\n\n## Assigned tasks\nTask 1"
        result1 = mgr.process_prompt(prompt1)

        prompt2 = "System prompt\n\n## Assigned tasks\nTask 2"
        result2 = mgr.process_prompt(prompt2)

        assert result1.cache_key == result2.cache_key
        assert result1.is_new_prefix is True
        assert result2.is_new_prefix is False
        assert result2.hit_count == 1


def test_caching_manager_persists_manifest() -> None:
    """Manager writes cache manifest to .sdd/caching/manifest.jsonl."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)
        mgr = PromptCachingManager(workdir)

        prompt = "You are a backend engineer.\n\n## Assigned tasks\nTask 1"
        mgr.process_prompt(prompt)
        mgr.save_manifest()

        manifest_path = workdir / ".sdd" / "caching" / "manifest.jsonl"
        assert manifest_path.exists()

        with open(manifest_path) as f:
            line = f.read().strip()
        assert "backend" in line or "cache_key" in line


def test_caching_manager_loads_persisted_manifest() -> None:
    """Manager loads persisted manifest on construction."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)

        mgr1 = PromptCachingManager(workdir)
        prompt = "System prompt\n\n## Assigned tasks\nTask 1"
        mgr1.process_prompt(prompt)
        mgr1.save_manifest()

        # Second manager loads the persisted manifest
        mgr2 = PromptCachingManager(workdir)
        result = mgr2.process_prompt(prompt)
        assert result.is_new_prefix is False  # already known from persisted manifest


def test_caching_manager_statistics() -> None:
    """Manager returns accurate statistics dict."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)
        mgr = PromptCachingManager(workdir)

        prompt1 = "System prompt\n\n## Assigned tasks\nTask 1"
        mgr.process_prompt(prompt1)

        prompt2 = "System prompt\n\n## Assigned tasks\nTask 2"
        mgr.process_prompt(prompt2)

        stats = mgr.get_statistics()
        assert "cache_entries" in stats
        assert "total_cached_requests" in stats
        assert stats["cache_entries"] == 1
        assert stats["total_cached_requests"] == 1


def test_estimate_tokens() -> None:
    """Token estimator returns roughly len//4, minimum 1."""
    assert _estimate_tokens("") == 1  # minimum guard
    assert _estimate_tokens("a" * 40) == 10
    long_text = "x" * 4000
    assert _estimate_tokens(long_text) == 1000


def test_process_prompt_sets_prefix_tokens() -> None:
    """New cache entries store non-zero prefix_tokens estimate."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = PromptCachingManager(Path(tmpdir))
        prefix = "You are a backend engineer with deep knowledge of databases."
        prompt = f"{prefix}\n\n## Assigned tasks\nTask 1"
        result = mgr.process_prompt(prompt)

        assert result.is_new_prefix
        entry = mgr._manifest.entries[result.cache_key]
        assert entry.prefix_tokens > 0
        assert entry.prefix_tokens == _estimate_tokens(prefix)


def test_total_cached_tokens_accumulates_on_hits() -> None:
    """total_cached_tokens grows by prefix_tokens on each cache hit."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = PromptCachingManager(Path(tmpdir))
        prefix = "You are a backend engineer."
        result1 = mgr.process_prompt(f"{prefix}\n\n## Assigned tasks\nTask 1")

        assert mgr._manifest.total_cached_tokens == 0  # first hit — no savings yet

        # expected_tokens comes from the entry (actual extracted prefix, not raw prefix)
        entry = mgr._manifest.entries[result1.cache_key]
        expected_tokens = entry.prefix_tokens
        assert expected_tokens > 0

        mgr.process_prompt(f"{prefix}\n\n## Assigned tasks\nTask 2")
        assert mgr._manifest.total_cached_tokens == expected_tokens

        mgr.process_prompt(f"{prefix}\n\n## Assigned tasks\nTask 3")
        assert mgr._manifest.total_cached_tokens == expected_tokens * 2


def test_statistics_include_savings() -> None:
    """get_statistics() returns total_cached_tokens and estimated_savings_usd."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = PromptCachingManager(Path(tmpdir))
        prefix = "You are a backend engineer."
        mgr.process_prompt(f"{prefix}\n\n## Assigned tasks\nT1")
        mgr.process_prompt(f"{prefix}\n\n## Assigned tasks\nT2")

        stats = mgr.get_statistics()
        assert "total_cached_tokens" in stats
        assert "estimated_savings_usd" in stats
        assert stats["total_cached_tokens"] > 0
        assert stats["estimated_savings_usd"] > 0.0


def test_backfill_token_estimates_on_load() -> None:
    """Reloaded manager backfills prefix_tokens=0 entries from system_prefix."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)

        # Write a manifest with prefix_tokens=0 (old format)
        manifest_path = workdir / ".sdd" / "caching" / "manifest.jsonl"
        manifest_path.parent.mkdir(parents=True)
        old_entry = {
            "cache_key": "abc123",
            "system_prefix": "a" * 400,  # 100 estimated tokens
            "prefix_tokens": 0,
            "hit_count": 5,
            "first_seen_at": 1000.0,
            "last_used_at": 2000.0,
        }
        manifest_data = {
            "entries": {"abc123": old_entry},
            "total_cached_tokens": 0,
            "total_cached_requests": 5,
        }
        import json as _json

        manifest_path.write_text(_json.dumps(manifest_data))

        mgr = PromptCachingManager(workdir)
        entry = mgr._manifest.entries["abc123"]
        assert entry.prefix_tokens == 100  # backfilled: 400 chars // 4


# ---------------------------------------------------------------------------
# CachingAdapter
# ---------------------------------------------------------------------------


def test_caching_adapter_wraps_spawn() -> None:
    """CachingAdapter wraps inner adapter spawn call."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)

        mock_adapter = Mock(spec=CLIAdapter)
        mock_result = SpawnResult(pid=12345, log_path=workdir / "test.log")
        mock_adapter.spawn.return_value = mock_result
        mock_adapter.name.return_value = "MockAdapter"

        caching = CachingAdapter(mock_adapter, workdir)

        config = ModelConfig(model="sonnet", effort="high")
        result = caching.spawn(
            prompt="You are a backend engineer.\n\n## Assigned tasks\nTask 1",
            workdir=workdir,
            model_config=config,
            session_id="backend-abc123",
        )

        assert mock_adapter.spawn.called
        assert result.pid == 12345


def test_caching_adapter_saves_manifest_on_spawn() -> None:
    """CachingAdapter persists manifest after each spawn."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)

        mock_adapter = Mock(spec=CLIAdapter)
        mock_adapter.spawn.return_value = SpawnResult(pid=1, log_path=workdir / "log")

        caching = CachingAdapter(mock_adapter, workdir)
        config = ModelConfig(model="sonnet", effort="high")
        caching.spawn(
            prompt="System\n\n## Assigned tasks\nT1",
            workdir=workdir,
            model_config=config,
            session_id="s1",
        )

        manifest_path = workdir / ".sdd" / "caching" / "manifest.jsonl"
        assert manifest_path.exists()


def test_caching_adapter_delegates_name() -> None:
    """CachingAdapter.name() delegates to inner adapter."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mock_adapter = Mock(spec=CLIAdapter)
        mock_adapter.name.return_value = "claude"
        caching = CachingAdapter(mock_adapter, Path(tmpdir))
        assert caching.name() == "claude"


# ---------------------------------------------------------------------------
# AgentSpawner integration
# ---------------------------------------------------------------------------


def test_agent_spawner_uses_caching_adapter() -> None:
    """AgentSpawner wraps its adapter with CachingAdapter if enabled."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)
        templates_dir = workdir / "templates" / "roles"
        templates_dir.mkdir(parents=True, exist_ok=True)

        from bernstein.core.spawner import AgentSpawner

        mock_adapter = Mock(spec=CLIAdapter)

        spawner = AgentSpawner(
            adapter=mock_adapter,
            templates_dir=templates_dir,
            workdir=workdir,
            enable_caching=True,
        )

        assert isinstance(spawner._adapter, CachingAdapter)


def test_agent_spawner_without_caching_is_unwrapped() -> None:
    """AgentSpawner without enable_caching uses the raw adapter."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)
        templates_dir = workdir / "templates" / "roles"
        templates_dir.mkdir(parents=True, exist_ok=True)

        from bernstein.core.spawner import AgentSpawner

        mock_adapter = Mock(spec=CLIAdapter)

        spawner = AgentSpawner(
            adapter=mock_adapter,
            templates_dir=templates_dir,
            workdir=workdir,
            enable_caching=False,
        )

        assert not isinstance(spawner._adapter, CachingAdapter)
        assert spawner._adapter is mock_adapter


def test_bootstrap_creates_spawner_with_caching() -> None:
    """Spawner created with enable_caching=True wraps inner adapter."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)
        templates_dir = workdir / "templates" / "roles"
        templates_dir.mkdir(parents=True, exist_ok=True)

        from bernstein.core.spawner import AgentSpawner

        mock_adapter = Mock(spec=CLIAdapter)

        spawner = AgentSpawner(
            adapter=mock_adapter,
            templates_dir=templates_dir,
            workdir=workdir,
            enable_caching=True,
        )

        assert isinstance(spawner._adapter, CachingAdapter)
        assert spawner._adapter._inner is mock_adapter


# ---------------------------------------------------------------------------
# make_prompt_cache_key — orchestrator-level cache key with file invalidation
# ---------------------------------------------------------------------------


def test_make_prompt_cache_key_text_only() -> None:
    """Cache key from system prompt alone is a 64-char hex SHA-256."""
    key = make_prompt_cache_key("You are a backend engineer.")
    assert isinstance(key, str)
    assert len(key) == 64


def test_make_prompt_cache_key_deterministic() -> None:
    """Same system prompt always produces the same key."""
    prompt = "You are a backend engineer with deep knowledge of databases."
    assert make_prompt_cache_key(prompt) == make_prompt_cache_key(prompt)


def test_make_prompt_cache_key_differs_for_different_prompts() -> None:
    """Different system prompts produce different keys."""
    k1 = make_prompt_cache_key("You are a backend engineer.")
    k2 = make_prompt_cache_key("You are a QA engineer.")
    assert k1 != k2


def test_make_prompt_cache_key_includes_context_files() -> None:
    """Key changes when context files are included vs not."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ctx = Path(tmpdir) / "context.md"
        ctx.write_text("# Project context\nThis is a Python service.")

        prompt = "You are a backend engineer."
        key_no_files = make_prompt_cache_key(prompt)
        key_with_files = make_prompt_cache_key(prompt, context_files=[ctx])

        assert key_no_files != key_with_files


def test_make_prompt_cache_key_invalidates_on_file_change() -> None:
    """Cache key changes when a context file's content changes."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ctx = Path(tmpdir) / "context.md"
        ctx.write_text("# Project context v1")

        prompt = "You are a backend engineer."
        key_v1 = make_prompt_cache_key(prompt, context_files=[ctx])

        ctx.write_text("# Project context v2 — changed")
        key_v2 = make_prompt_cache_key(prompt, context_files=[ctx])

        assert key_v1 != key_v2


def test_make_prompt_cache_key_stable_when_files_unchanged() -> None:
    """Cache key is stable across calls when context files are unchanged."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ctx = Path(tmpdir) / "context.md"
        ctx.write_text("# Stable context")

        prompt = "You are a backend engineer."
        k1 = make_prompt_cache_key(prompt, context_files=[ctx])
        k2 = make_prompt_cache_key(prompt, context_files=[ctx])

        assert k1 == k2


def test_make_prompt_cache_key_missing_file_is_skipped() -> None:
    """Non-existent context files are skipped without error."""
    prompt = "You are a backend engineer."
    key = make_prompt_cache_key(prompt, context_files=[Path("/nonexistent/file.md")])
    # Should not raise, key is still a valid 64-char hash
    assert len(key) == 64


def test_make_prompt_cache_key_multiple_files() -> None:
    """Key is deterministic when multiple context files are provided."""
    with tempfile.TemporaryDirectory() as tmpdir:
        f1 = Path(tmpdir) / "a.md"
        f2 = Path(tmpdir) / "b.md"
        f1.write_text("file A content")
        f2.write_text("file B content")

        prompt = "You are a backend engineer."
        k1 = make_prompt_cache_key(prompt, context_files=[f1, f2])
        k2 = make_prompt_cache_key(prompt, context_files=[f2, f1])  # order-independent

        assert k1 == k2  # sorted by path, so order-independent
        assert len(k1) == 64


# ---------------------------------------------------------------------------
# CacheBreakEvent
# ---------------------------------------------------------------------------


def test_cache_break_event_roundtrip() -> None:
    """Cache break event serializes and deserializes cleanly."""
    event = CacheBreakEvent(
        timestamp=1234567890.0,
        reason=CacheBreakReason.SYSTEM,
        old_cache_key="old_key_123",
        new_cache_key="new_key_456",
        estimated_token_delta=150,
        session_id="session-abc",
        model_name="claude-sonnet-4-20250514",
        provider_name="anthropic",
    )
    data = event.to_dict()
    assert data["reason"] == "system"
    assert data["model_name"] == "claude-sonnet-4-20250514"

    restored = CacheBreakEvent.from_dict(data)
    assert restored.reason == CacheBreakReason.SYSTEM
    assert restored.model_name == "claude-sonnet-4-20250514"


def test_cache_break_event_json_line() -> None:
    """JSON line serialization is valid JSON."""
    event = CacheBreakEvent(
        timestamp=1234567890.0,
        reason=CacheBreakReason.TOOLS,
        old_cache_key=None,
        new_cache_key="key_123",
        estimated_token_delta=200,
        session_id="sess-1",
    )
    line = event.to_json_line()
    parsed = json.loads(line)
    assert parsed["reason"] == "tools"
    assert parsed["old_cache_key"] is None


def test_all_cache_break_reasons_roundtrip() -> None:
    """Every CacheBreakReason survives serialization."""
    for reason in CacheBreakReason:
        event = CacheBreakEvent(
            timestamp=0.0,
            reason=reason,
            old_cache_key=None,
            new_cache_key="x",
            estimated_token_delta=0,
            session_id="s",
        )
        restored = CacheBreakEvent.from_dict(event.to_dict())
        assert restored.reason == reason


def test_prompt_process_result_includes_new_fields() -> None:
    """PromptProcessResult includes first_seen and prefix_tokens."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = PromptCachingManager(Path(tmpdir))
        prompt = "You are a backend engineer.\n\n## Assigned tasks\n"
        result = mgr.process_prompt(prompt)
        assert result.is_new_prefix
        assert result.prefix_tokens > 0
        assert result.first_seen is not None
        assert isinstance(result.first_seen, float)


def test_cache_break_event_emitted_on_new_prefix(tmp_path: Path) -> None:
    """CachingAdapter writes a cache break event when encountering a new prefix."""
    inner = Mock(spec=CLIAdapter)
    inner.name.return_value = "backend"
    inner.spawn.return_value = SpawnResult(pid=42, log_path=tmp_path / "test.log")

    workdir = tmp_path / "project"
    workdir.mkdir()
    adapter = CachingAdapter(inner, workdir)

    model_cfg = Mock(spec=ModelConfig)
    model_cfg.model_name = "claude-sonnet-4-20250514"
    model_cfg.provider = "anthropic"

    prompt = "You are a QA engineer.\n\n## Assigned tasks\n"
    result = adapter.spawn(
        prompt=prompt,
        workdir=tmp_path,
        model_config=model_cfg,
        session_id="sess-qa-001",
    )

    assert result.pid == 42  # went to inner adapter

    break_file = workdir / ".sdd" / "metrics" / "cache_breaks.jsonl"
    assert break_file.exists()
    import json

    lines = break_file.read_text().strip().splitlines()
    assert len(lines) == 1
    event_data = json.loads(lines[0])
    assert event_data["reason"] == "system"
    assert event_data["session_id"] == "sess-qa-001"
    assert event_data["model_name"] == "claude-sonnet-4-20250514"


def test_no_cache_break_event_on_cache_hit(tmp_path: Path) -> None:
    """CachingAdapter does NOT emit a cache break event when prefix is reused."""
    inner = Mock(spec=CLIAdapter)
    inner.name.return_value = "backend"
    inner.spawn.return_value = SpawnResult(pid=42, log_path=tmp_path / "test.log")

    workdir = tmp_path / "project"
    workdir.mkdir()
    adapter = CachingAdapter(inner, workdir)

    model_cfg = Mock(spec=ModelConfig)
    model_cfg.model_name = "claude-sonnet-4-20250514"
    model_cfg.provider = "anthropic"

    prompt = "You are a backend engineer.\n\n## Assigned tasks\n"
    # First call: NEW prefix (emit break event)
    adapter.spawn(
        prompt=prompt,
        workdir=tmp_path,
        model_config=model_cfg,
        session_id="sess-1",
    )

    # Second call: SAME prefix (cache HIT, no break event)
    adapter.spawn(
        prompt=prompt,
        workdir=tmp_path,
        model_config=model_cfg,
        session_id="sess-2",
    )

    break_file = workdir / ".sdd" / "metrics" / "cache_breaks.jsonl"
    lines = break_file.read_text().strip().splitlines()
    assert len(lines) == 1  # Only the first call emitted a break


def test_expected_drop_does_not_emit_cache_break(tmp_path: Path) -> None:
    """CachingAdapter does NOT write a break event when mark_expected_drop was called."""
    inner = Mock(spec=CLIAdapter)
    inner.name.return_value = "backend"
    inner.spawn.return_value = SpawnResult(pid=42, log_path=tmp_path / "test.log")

    workdir = tmp_path / "project"
    workdir.mkdir()
    adapter = CachingAdapter(inner, workdir)

    model_cfg = Mock(spec=ModelConfig)
    model_cfg.model_name = "claude-sonnet-4-20250514"
    model_cfg.provider = "anthropic"

    # Pre-announce expected drop (simulating compaction or manual cache clear)
    adapter._caching_mgr.mark_expected_drop("compaction")

    prompt = "You are a security engineer.\n\n## Assigned tasks\n"
    adapter.spawn(
        prompt=prompt,
        workdir=tmp_path,
        model_config=model_cfg,
        session_id="sess-expected",
    )

    break_file = workdir / ".sdd" / "metrics" / "cache_breaks.jsonl"
    # No break event file at all, since expected drops are suppressed
    assert not break_file.exists()


def test_unexpected_drop_still_emits_break(tmp_path: Path) -> None:
    """CachingAdapter writes a break event when prefix changes without pre-announcement."""
    inner = Mock(spec=CLIAdapter)
    inner.name.return_value = "backend"
    inner.spawn.return_value = SpawnResult(pid=42, log_path=tmp_path / "test.log")

    workdir = tmp_path / "project"
    workdir.mkdir()
    adapter = CachingAdapter(inner, workdir)

    model_cfg = Mock(spec=ModelConfig)
    model_cfg.model_name = "claude-sonnet-4-20250514"
    model_cfg.provider = "anthropic"

    # First: establish baseline
    adapter.spawn(
        prompt="You are a backend engineer.\n\n## Assigned tasks\n",
        workdir=tmp_path,
        model_config=model_cfg,
        session_id="sess-base",
    )

    # Second: new prefix without pre-announcement -> surprise break
    adapter.spawn(
        prompt="You are a security engineer.\n\n## Assigned tasks\n",
        workdir=tmp_path,
        model_config=model_cfg,
        session_id="sess-surprise",
    )

    break_file = workdir / ".sdd" / "metrics" / "cache_breaks.jsonl"
    assert break_file.exists()
    text = break_file.read_text()
    assert "sess-surprise" in text


# ---------------------------------------------------------------------------
# CacheSafeParams & build_cache_safe_fork_params (T446)
# ---------------------------------------------------------------------------


class TestCacheSafeParams:
    """Tests for the CacheSafeParams typed structure."""

    def test_defaults(self) -> None:
        """CacheSafeParams has sensible defaults."""
        params = CacheSafeParams(
            inherited_cache_key="abc123",
            system_prefix="You are an engineer.",
        )
        assert params.fork_role == ""
        assert params.fork_model == ""
        assert params.fork_messages == []
        assert params.cache_safe is True

    def test_to_dict_includes_all_fields(self) -> None:
        """to_dict() produces all expected keys."""
        msg = [{"role": "user", "content": "hello"}]
        params = CacheSafeParams(
            inherited_cache_key="key1",
            system_prefix="prefix",
            fork_role="qa",
            fork_model="haiku",
            fork_messages=msg,
            cache_safe=True,
        )
        d = params.to_dict()
        assert d["inherited_cache_key"] == "key1"
        assert d["system_prefix"] == "prefix"
        assert d["role"] == "qa"
        assert d["model"] == "haiku"
        assert d["fork_messages"] == msg
        assert d["cache_safe"] is True

    def test_to_dict_omits_empty_fork_messages(self) -> None:
        """to_dict() omits fork_messages when it is an empty list."""
        params = CacheSafeParams(
            inherited_cache_key="key1",
            system_prefix="prefix",
        )
        d = params.to_dict()
        assert "fork_messages" not in d

    def test_from_dict_roundtrip(self) -> None:
        """from_dict() reconstructs the original instance."""
        msg = [{"role": "assistant", "content": "done"}]
        original = CacheSafeParams(
            inherited_cache_key="ck",
            system_prefix="sp",
            fork_role="devops",
            fork_model="opus",
            fork_messages=msg,
            cache_safe=True,
        )
        reconstructed = CacheSafeParams.from_dict(original.to_dict())
        assert reconstructed.inherited_cache_key == original.inherited_cache_key
        assert reconstructed.system_prefix == original.system_prefix
        assert reconstructed.fork_role == original.fork_role
        assert reconstructed.fork_model == original.fork_model
        assert reconstructed.fork_messages == original.fork_messages
        assert reconstructed.cache_safe == original.cache_safe

    def test_from_dict_defaults_correct(self) -> None:
        """from_dict() handles missing optional keys gracefully."""
        data = {
            "inherited_cache_key": "k",
            "system_prefix": "p",
        }
        params = CacheSafeParams.from_dict(data)
        assert params.fork_role == ""
        assert params.fork_model == ""
        assert params.fork_messages == []
        assert params.cache_safe is True

    def test_is_frozen(self) -> None:
        """CacheSafeParams is immutable (frozen dataclass)."""
        params = CacheSafeParams(
            inherited_cache_key="k",
            system_prefix="p",
        )
        with pytest.raises(FrozenInstanceError):
            params.fork_role = "new"  # type: ignore[misc]


class TestBuildCacheSafeForkParams:
    """Tests for the build_cache_safe_fork_params helper."""

    def test_returns_cache_safe_params(self) -> None:
        """Returns a CacheSafeParams with the provided values."""
        msgs = [{"role": "user", "content": "fix this"}]
        result = build_cache_safe_fork_params(
            parent_cache_key="parent_key",
            parent_system_prefix="parent_prefix",
            fork_role="debugger",
            fork_model="sonnet",
            fork_messages=msgs,
        )
        assert result.inherited_cache_key == "parent_key"
        assert result.system_prefix == "parent_prefix"
        assert result.fork_role == "debugger"
        assert result.fork_model == "sonnet"
        assert result.fork_messages == msgs
        assert result.cache_safe is True

    def test_defaults_to_empty_role_and_model(self) -> None:
        """role and model default to empty strings for parent inheritance."""
        result = build_cache_safe_fork_params(
            parent_cache_key="pk",
            parent_system_prefix="ps",
        )
        assert result.fork_role == ""
        assert result.fork_model == ""
        assert result.fork_messages == []

    def test_default_empty_fork_messages(self) -> None:
        """fork_messages defaults to empty list when not provided."""
        result = build_cache_safe_fork_params(
            parent_cache_key="pk",
            parent_system_prefix="ps",
        )
        assert result.fork_messages == []

    def test_stable_cache_key_alignment(self) -> None:
        """Inherited cache key matches the compute_cache_key of the system prefix."""
        system_prefix = "You are a senior backend engineer.\n\n## Context\n"
        expected_key = compute_cache_key(system_prefix)
        result = build_cache_safe_fork_params(
            parent_cache_key=expected_key,
            parent_system_prefix=system_prefix,
        )
        assert result.inherited_cache_key == expected_key
        assert result.system_prefix == system_prefix


# ---------------------------------------------------------------------------
# AgentCacheTracker — per-agent FIFO eviction
# ---------------------------------------------------------------------------


class TestAgentCacheTracker:
    """Tests for AgentCacheTracker with FIFO eviction."""

    def test_record_and_get_single_entry(self) -> None:
        """record() stores a session; get() retrieves it."""
        tracker = AgentCacheTracker()
        evicted = tracker.record("agent-001", "key-abc")
        assert evicted is None
        assert tracker.get("agent-001") == "key-abc"

    def test_len_and_contains(self) -> None:
        """__len__ and __contains__ reflect tracker state."""
        tracker = AgentCacheTracker()
        assert len(tracker) == 0
        assert "agent-001" not in tracker
        tracker.record("agent-001", "key-abc")
        assert len(tracker) == 1
        assert "agent-001" in tracker

    def test_update_existing_entry_no_eviction(self) -> None:
        """Updating an existing session does not evict any entry."""
        tracker = AgentCacheTracker(max_entries=2)
        tracker.record("a", "key-1")
        tracker.record("b", "key-2")
        # Update "a" — should not evict anything
        evicted = tracker.record("a", "key-1-updated")
        assert evicted is None
        assert len(tracker) == 2
        assert tracker.get("a") == "key-1-updated"

    def test_fifo_eviction_at_cap(self) -> None:
        """When cap is exceeded, the first-inserted session is evicted."""
        tracker = AgentCacheTracker(max_entries=3)
        tracker.record("first", "k1")
        tracker.record("second", "k2")
        tracker.record("third", "k3")
        # Adding a 4th evicts "first"
        evicted = tracker.record("fourth", "k4")
        assert evicted == "first"
        assert tracker.get("first") is None
        assert tracker.get("fourth") == "k4"
        assert len(tracker) == 3

    def test_insert_11_agents_first_is_evicted(self) -> None:
        """Inserting 11 agents with default cap evicts the first one."""
        tracker = AgentCacheTracker()  # default max_entries=10
        assert tracker.max_entries == MAX_AGENT_CACHE_ENTRIES == 10

        for i in range(10):
            evicted = tracker.record(f"agent-{i:02d}", f"key-{i}")
            assert evicted is None  # no eviction until cap is hit

        assert len(tracker) == 10

        # 11th agent triggers eviction of agent-00 (first inserted)
        evicted = tracker.record("agent-10", "key-10")
        assert evicted == "agent-00"
        assert tracker.get("agent-00") is None
        assert tracker.get("agent-10") == "key-10"
        assert len(tracker) == 10

    def test_fifo_order_is_deterministic(self) -> None:
        """FIFO eviction is strictly ordered by insertion time, not update time."""
        tracker = AgentCacheTracker(max_entries=3)
        tracker.record("alpha", "k-a")
        tracker.record("beta", "k-b")
        tracker.record("gamma", "k-c")
        # Update "alpha" — does NOT change its eviction priority
        tracker.record("alpha", "k-a-new")
        # Adding "delta" should evict "alpha" (still oldest by insertion)
        evicted = tracker.record("delta", "k-d")
        assert evicted == "alpha"
        assert len(tracker) == 3

    def test_remove_session(self) -> None:
        """remove() drops the session; subsequent get() returns None."""
        tracker = AgentCacheTracker()
        tracker.record("agent-x", "key-x")
        assert "agent-x" in tracker
        tracker.remove("agent-x")
        assert tracker.get("agent-x") is None
        assert "agent-x" not in tracker

    def test_remove_nonexistent_is_noop(self) -> None:
        """remove() on an untracked session does not raise."""
        tracker = AgentCacheTracker()
        tracker.remove("nonexistent")  # must not raise

    def test_sequential_evictions_preserve_fifo(self) -> None:
        """Multiple insertions beyond cap evict in strictly FIFO order."""
        tracker = AgentCacheTracker(max_entries=3)
        for i in range(3):
            tracker.record(f"s{i}", f"k{i}")

        evicted_order = []
        for i in range(3, 6):
            evicted = tracker.record(f"s{i}", f"k{i}")
            assert evicted is not None
            evicted_order.append(evicted)

        assert evicted_order == ["s0", "s1", "s2"]

    def test_no_unbounded_growth_stress(self) -> None:
        """Stress test: tracker never exceeds max_entries regardless of insertion count."""
        tracker = AgentCacheTracker(max_entries=10)
        for i in range(1000):
            tracker.record(f"agent-{i}", f"key-{i}")
            assert len(tracker) <= 10

    def test_get_returns_none_for_unknown_session(self) -> None:
        """get() returns None for sessions that were never recorded."""
        tracker = AgentCacheTracker()
        assert tracker.get("unknown") is None


# ---------------------------------------------------------------------------
# PromptCachingManager — per-agent tracker integration
# ---------------------------------------------------------------------------


class TestPromptCachingManagerAgentTracking:
    """Tests for PromptCachingManager.process_prompt with session_id."""

    def test_process_prompt_with_session_id_updates_tracker(self, tmp_path: Path) -> None:
        """process_prompt with session_id records the agent's cache key."""
        mgr = PromptCachingManager(tmp_path)
        prompt = "You are a backend engineer.\n\n## Assigned tasks\nT1"
        result = mgr.process_prompt(prompt, session_id="backend-abc")
        assert mgr._agent_tracker.get("backend-abc") == result.cache_key

    def test_process_prompt_without_session_id_skips_tracker(self, tmp_path: Path) -> None:
        """process_prompt without session_id leaves tracker empty."""
        mgr = PromptCachingManager(tmp_path)
        mgr.process_prompt("prompt\n\n## Assigned tasks\nT1")
        assert len(mgr._agent_tracker) == 0

    def test_tracker_evicts_oldest_after_11_spawns(self, tmp_path: Path) -> None:
        """Spawning 11 distinct sessions evicts the first one from the tracker."""
        mgr = PromptCachingManager(tmp_path)
        base_prompt = "## Assigned tasks\nT"
        # Use distinct system prefixes per agent to give each a unique cache key
        for i in range(10):
            mgr.process_prompt(
                f"Role {i}\n\n{base_prompt}{i}",
                session_id=f"agent-{i:02d}",
            )
        assert len(mgr._agent_tracker) == 10

        mgr.process_prompt("Role 10\n\n## Assigned tasks\nT10", session_id="agent-10")
        assert len(mgr._agent_tracker) == 10
        assert mgr._agent_tracker.get("agent-00") is None
        assert mgr._agent_tracker.get("agent-10") is not None

    def test_statistics_include_tracker_fields(self, tmp_path: Path) -> None:
        """get_statistics() includes tracked_agents and max_tracked_agents."""
        mgr = PromptCachingManager(tmp_path)
        mgr.process_prompt("prompt\n\n## Assigned tasks\nT1", session_id="s1")
        stats = mgr.get_statistics()
        assert "tracked_agents" in stats
        assert "max_tracked_agents" in stats
        assert stats["tracked_agents"] == 1
        assert stats["max_tracked_agents"] == MAX_AGENT_CACHE_ENTRIES
