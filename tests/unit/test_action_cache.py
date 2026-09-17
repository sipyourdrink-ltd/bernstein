"""Unit tests for the action-level cache.

Covers:
* key derivation determinism (same inputs → same key, different inputs → different)
* secret redaction in prompts before persistence
* record/replay round-trip via MemoStore
* mode behaviour: ``record``, ``replay`` (raises on miss), ``hybrid``, ``off``
* MemoStore eviction holds under 1000-entry stress
* Prometheus hit counter increments on cache hit
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bernstein.core.persistence.action_cache import (
    ActionCache,
    CacheMiss,
    TokenCounts,
    default_store,
    derive_key,
    open_cache,
    redact_secrets,
)
from bernstein.core.persistence.fingerprint import MemoStore

# ---------------------------------------------------------------------------
# Key derivation
# ---------------------------------------------------------------------------


class TestDeriveKey:
    def test_same_inputs_same_key(self) -> None:
        a = derive_key(model_id="claude-opus-4-7", prompt="hello world")
        b = derive_key(model_id="claude-opus-4-7", prompt="hello world")
        assert a == b

    def test_digest_is_32_bytes(self) -> None:
        assert len(derive_key(model_id="m", prompt="p")) == 32

    def test_different_model_different_key(self) -> None:
        a = derive_key(model_id="opus", prompt="hello")
        b = derive_key(model_id="haiku", prompt="hello")
        assert a != b

    def test_different_prompt_different_key(self) -> None:
        a = derive_key(model_id="m", prompt="hello")
        b = derive_key(model_id="m", prompt="hello!")
        assert a != b

    def test_tool_args_affect_key(self) -> None:
        a = derive_key(model_id="m", prompt="p", tool_name="bash", tool_args={"cmd": "ls"})
        b = derive_key(model_id="m", prompt="p", tool_name="bash", tool_args={"cmd": "pwd"})
        assert a != b

    def test_prompt_whitespace_normalized(self) -> None:
        a = derive_key(model_id="m", prompt="hello")
        b = derive_key(model_id="m", prompt="  hello  \n")
        assert a == b

    def test_secrets_redacted_in_key(self) -> None:
        # Two prompts that differ only by an API key MUST produce identical
        # keys - otherwise rotating creds invalidates the entire cache.
        a = derive_key(
            model_id="m",
            prompt="Authorization: Bearer sk-ant-AAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        )
        b = derive_key(
            model_id="m",
            prompt="Authorization: Bearer sk-ant-BBBBBBBBBBBBBBBBBBBBBBBBBBBB",
        )
        assert a == b


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


class TestRedactSecrets:
    @pytest.mark.parametrize(
        "raw, must_not_contain",
        [
            ("sk-ant-1234567890abcdefghij1234", "sk-ant-1234567890abcdefghij"),
            ("Authorization: Bearer abcdefghij1234567890", "abcdefghij1234567890"),
            ("ghp_1234567890abcdefghij1234567890ABCD", "ghp_1234567890abcdefghij"),
            ("X-API-Key: shhhhh-this-is-secret-1234", "shhhhh-this-is-secret"),
            ("AKIAABCDEFGHIJKLMNOP", "AKIAABCDEFGHIJKLMNOP"),
        ],
    )
    def test_known_patterns_redacted(self, raw: str, must_not_contain: str) -> None:
        out = redact_secrets(raw)
        assert must_not_contain not in out

    def test_innocuous_text_passes_through(self) -> None:
        assert redact_secrets("hello, world") == "hello, world"


# ---------------------------------------------------------------------------
# ActionCache record/replay round-trip
# ---------------------------------------------------------------------------


def _make_cache(tmp_path: Path, mode: str = "hybrid") -> ActionCache:
    store = MemoStore(root=tmp_path / "ac", max_mb=1)
    return ActionCache(store, mode=mode, run_id="run-test")  # type: ignore[arg-type]


class TestRecordReplayRoundTrip:
    def test_record_then_lookup_returns_same_payload(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        cache.record(
            model_id="opus",
            prompt="say hi",
            output_text="hi!",
            tokens=TokenCounts(prompt_tokens=4, completion_tokens=2),
            cost_usd=0.001,
        )
        rec = cache.lookup(model_id="opus", prompt="say hi")
        assert rec is not None
        assert rec.output_text == "hi!"
        assert rec.tokens.prompt_tokens == 4
        assert rec.cost_usd == pytest.approx(0.001)
        assert rec.run_id == "run-test"

    def test_lookup_miss_returns_none(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        assert cache.lookup(model_id="opus", prompt="never seen") is None

    def test_recorded_prompt_is_redacted_on_disk(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        cache.record(
            model_id="opus",
            prompt="Authorization: Bearer sk-ant-SECRETSECRETSECRETSECRET",
            output_text="ok",
        )
        # Read back and assert we never persisted the secret.
        rec = cache.lookup(
            model_id="opus",
            prompt="Authorization: Bearer sk-ant-DIFFERENTBUTSAMEKEYYYYYY",
        )
        assert rec is not None
        assert "sk-ant-SECRETSECRETSECRETSECRET" not in rec.prompt
        assert "sk-ant-DIFFERENTBUTSAMEKEYYYYYY" not in rec.prompt
        assert "REDACTED" in rec.prompt


class TestModes:
    def test_replay_mode_does_not_write(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path, mode="replay")
        cache.record(model_id="opus", prompt="p", output_text="o")
        # Same key, but in replay mode it should never have been persisted.
        assert cache.lookup(model_id="opus", prompt="p") is None

    def test_off_mode_skips_lookups(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path, mode="hybrid")
        cache.record(model_id="opus", prompt="p", output_text="o")

        cache_off = _make_cache(tmp_path, mode="off")
        # Even with a record on disk, off-mode returns None.
        assert cache_off.lookup(model_id="opus", prompt="p") is None

    def test_get_or_call_replay_miss_raises(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path, mode="replay")
        with pytest.raises(CacheMiss):
            cache.get_or_call(
                model_id="opus",
                prompt="never recorded",
                live_call=lambda: ("should-not-run", None, TokenCounts(), 0.0),
            )

    def test_get_or_call_hybrid_falls_through(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path, mode="hybrid")
        calls = {"n": 0}

        def live() -> tuple[str, None, TokenCounts, float]:
            calls["n"] += 1
            return ("live-out", None, TokenCounts(prompt_tokens=1), 0.05)

        rec1 = cache.get_or_call(model_id="opus", prompt="p", live_call=live)
        rec2 = cache.get_or_call(model_id="opus", prompt="p", live_call=live)
        assert rec1.output_text == "live-out"
        assert rec2.output_text == "live-out"
        assert calls["n"] == 1  # second call served from cache
        stats = cache.stats()
        assert stats.hits == 1
        assert stats.misses == 1
        assert stats.savings_usd == pytest.approx(0.05)


# ---------------------------------------------------------------------------
# Eviction stress (delegated to MemoStore - we just confirm the cap holds
# when ActionRecord payloads are written through ActionCache).
# ---------------------------------------------------------------------------


class TestEvictionStress:
    def test_thousand_records_respect_size_cap(self, tmp_path: Path) -> None:
        store = MemoStore(root=tmp_path / "ac", max_mb=1)
        store._max_bytes = 64 * 1024  # 64 KiB cap
        cache = ActionCache(store, mode="record", run_id="stress")
        big = "x" * 512
        for i in range(1000):
            cache.record(
                model_id="opus",
                prompt=f"prompt-{i}",
                output_text=big,
                cost_usd=0.0001,
            )
        assert store.total_bytes() <= store._max_bytes


# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------


class TestFactory:
    def test_default_store_uses_action_cache_dir(self, tmp_path: Path) -> None:
        store = default_store(tmp_path)
        assert store.root == tmp_path / ".sdd" / "runtime" / "action_cache"

    def test_open_cache_honours_explicit_mode(self, tmp_path: Path) -> None:
        cache = open_cache(tmp_path, mode="record", run_id="r1")
        assert cache.mode == "record"

    def test_open_cache_disabled_falls_back_to_off(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from bernstein.core import defaults as _d

        monkeypatch.setattr(
            _d,
            "ACTION_CACHE",
            type(_d.ACTION_CACHE)(enabled=False, mode="hybrid", size_mb=10),
        )
        cache = open_cache(tmp_path)
        assert cache.mode == "off"


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


@pytest.fixture
def cache_counters(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any]:
    """Give one test its own action-cache counters, built from the real client.

    Two separate problems make the module-level counters unusable here, and this
    closes both.

    The registry they live on is process-global, so their values depend on
    everything else that ran in the worker. A delta around the action answers
    that much.

    A delta does not answer the second: `_emit_hit_metric` resolves both
    counters by ``getattr`` on the prometheus module *at call time*, and that
    module is re-imported by
    `tests/unit/observability/test_prometheus_import_fallback.py`, which
    deliberately builds it under a simulated Windows import timeout -- where
    every metric is an inert stub that records nothing. Run this file after one
    of those in the same worker and the cache increments a stub while the
    assertion reads a stub, so the delta is zero with nothing wrong in the
    cache. That is the shape #5920 measured.

    Building the counters from `prometheus_client` directly, rather than from
    whatever `bernstein.core.observability.prometheus` currently exposes, makes
    this test independent of which version of that module is installed -- and
    they are still by construction the objects `_emit_hit_metric` will find,
    because it looks them up by name on the module these are patched onto.
    """
    prometheus_client = pytest.importorskip("prometheus_client")
    from bernstein.core.observability import prometheus as _p

    private = prometheus_client.CollectorRegistry(auto_describe=True)
    hits = prometheus_client.Counter(
        "bernstein_action_cache_hits_total",
        "Action-cache hits (test-local registry).",
        labelnames=["model"],
        registry=private,
    )
    savings = prometheus_client.Counter(
        "bernstein_action_cache_savings_usd",
        "USD saved by the action cache (test-local registry).",
        labelnames=["model"],
        registry=private,
    )
    monkeypatch.setattr(_p, "action_cache_hits_total", hits, raising=False)
    monkeypatch.setattr(_p, "action_cache_savings_usd_total", savings, raising=False)
    return hits, savings


class TestMetrics:
    def test_hit_increments_prometheus_counter(self, tmp_path: Path, cache_counters: tuple[Any, Any]) -> None:
        hits, _ = cache_counters

        cache = _make_cache(tmp_path)
        cache.record(model_id="opus", prompt="p", output_text="o", cost_usd=0.02)
        cache.lookup(model_id="opus", prompt="p")

        assert _sample_counter(hits, model="opus") == pytest.approx(1.0)

    def test_savings_counter_accumulates_cost(self, tmp_path: Path, cache_counters: tuple[Any, Any]) -> None:
        _, savings = cache_counters

        cache = _make_cache(tmp_path)
        cache.record(model_id="opus", prompt="q", output_text="o", cost_usd=0.13)
        cache.lookup(model_id="opus", prompt="q")

        assert _sample_counter(savings, model="opus") == pytest.approx(0.13)

    def test_a_miss_increments_nothing(self, tmp_path: Path, cache_counters: tuple[Any, Any]) -> None:
        """The counters start at zero here, so an absence is now assertable.

        Against the shared registry this could not be written at all: a
        non-zero starting value was indistinguishable from an increment.
        """
        hits, savings = cache_counters

        cache = _make_cache(tmp_path)
        cache.record(model_id="opus", prompt="p", output_text="o", cost_usd=0.02)
        cache.lookup(model_id="opus", prompt="a different prompt")

        assert _sample_counter(hits, model="opus") == pytest.approx(0.0)
        assert _sample_counter(savings, model="opus") == pytest.approx(0.0)


def _sample_counter(counter: Any, **labels: str) -> float:
    """Return current value of a labelled Prometheus counter (or 0.0)."""
    try:
        labelled = counter.labels(**labels)
        # prometheus_client exposes ._value.get() on Counter children
        value = labelled._value.get()
        return float(value)
    except Exception:
        return 0.0
