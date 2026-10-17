"""Tests for the managed credential pool (#6561)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from bernstein.core.credentials.pool import (
    CredentialEntry,
    CredentialPool,
    CredentialPoolError,
    NoAvailableCredentialError,
)


def _make_entry(key_id: str = "k1", provider: str = "openai", **kw):
    return CredentialEntry(
        key_id=key_id,
        provider=provider,
        secret_ref=f"vault:{key_id}",
        **kw,
    )


class TestCredentialEntry:
    def test_available_by_default(self):
        e = _make_entry()
        assert e.is_available

    def test_not_available_during_cooldown(self):
        e = _make_entry(cooldown_until_ts=time.time() + 3600)
        assert not e.is_available

    def test_not_available_when_exhausted(self):
        e = _make_entry(remaining_quota=0)
        assert not e.is_available

    def test_available_after_cooldown_expires(self):
        e = _make_entry(cooldown_until_ts=time.time() - 1)
        assert e.is_available

    def test_unlimited_quota_is_available(self):
        e = _make_entry(remaining_quota=-1)
        assert e.is_available


class TestCredentialPoolCRUD:
    def test_add_and_get(self):
        pool = CredentialPool("test")
        entry = _make_entry()
        pool.add(entry)
        assert pool.get("k1") is entry

    def test_add_duplicate_raises(self):
        pool = CredentialPool("test")
        pool.add(_make_entry())
        with pytest.raises(CredentialPoolError, match="already exists"):
            pool.add(_make_entry())

    def test_remove(self):
        pool = CredentialPool("test")
        pool.add(_make_entry())
        removed = pool.remove("k1")
        assert removed.key_id == "k1"
        with pytest.raises(KeyError):
            pool.get("k1")

    def test_update(self):
        pool = CredentialPool("test")
        pool.add(_make_entry())
        updated = pool.update("k1", remaining_quota=100)
        assert updated.remaining_quota == 100

    def test_update_invalid_field_raises(self):
        pool = CredentialPool("test")
        pool.add(_make_entry())
        with pytest.raises(CredentialPoolError, match="no field"):
            pool.update("k1", nonexistent_field=42)

    def test_keys(self):
        pool = CredentialPool("test")
        pool.add(_make_entry("a"))
        pool.add(_make_entry("b"))
        assert sorted(pool.keys()) == ["a", "b"]


class TestAcquireRelease:
    def test_acquire_returns_entry(self):
        pool = CredentialPool("test")
        pool.add(_make_entry())
        acquired = pool.acquire()
        assert acquired.key_id == "k1"
        assert acquired.usage_count == 1

    def test_acquire_least_used(self):
        pool = CredentialPool("test")
        pool.add(_make_entry("a", usage_count=10))
        pool.add(_make_entry("b", usage_count=1))
        pool.add(_make_entry("c", usage_count=5))
        acquired = pool.acquire()
        assert acquired.key_id == "b"

    def test_acquire_skips_cooling_down(self):
        pool = CredentialPool("test")
        pool.add(_make_entry("a", cooldown_until_ts=time.time() + 3600))
        pool.add(_make_entry("b"))
        acquired = pool.acquire()
        assert acquired.key_id == "b"

    def test_acquire_skips_exhausted(self):
        pool = CredentialPool("test")
        pool.add(_make_entry("a", remaining_quota=0))
        pool.add(_make_entry("b"))
        acquired = pool.acquire()
        assert acquired.key_id == "b"

    def test_acquire_empty_pool_raises(self):
        pool = CredentialPool("test")
        with pytest.raises(NoAvailableCredentialError):
            pool.acquire()

    def test_acquire_all_unavailable_raises(self):
        pool = CredentialPool("test")
        pool.add(_make_entry("a", remaining_quota=0))
        pool.add(_make_entry("b", cooldown_until_ts=time.time() + 3600))
        with pytest.raises(NoAvailableCredentialError):
            pool.acquire()

    def test_acquire_by_provider(self):
        pool = CredentialPool("test")
        pool.add(_make_entry("a", provider="openai"))
        pool.add(_make_entry("b", provider="anthropic"))
        acquired = pool.acquire(provider="anthropic")
        assert acquired.key_id == "b"

    def test_release_rate_limited(self):
        pool = CredentialPool("test", cooldown_seconds=120)
        pool.add(_make_entry())
        pool.acquire()
        pool.release("k1", rate_limited=True)
        entry = pool.get("k1")
        assert entry.cooldown_until_ts > time.time()
        assert entry.rate_limit_count == 1

    def test_release_exhausted(self):
        pool = CredentialPool("test")
        pool.add(_make_entry(remaining_quota=100))
        pool.acquire()
        pool.release("k1", exhausted=True)
        assert pool.get("k1").remaining_quota == 0

    def test_release_quota_consumed(self):
        pool = CredentialPool("test")
        pool.add(_make_entry(remaining_quota=100))
        pool.acquire()
        pool.release("k1", quota_consumed=30)
        assert pool.get("k1").remaining_quota == 70

    def test_release_quota_does_not_go_negative(self):
        pool = CredentialPool("test")
        pool.add(_make_entry(remaining_quota=10))
        pool.acquire()
        pool.release("k1", quota_consumed=999)
        assert pool.get("k1").remaining_quota == 0


class TestPoolStatus:
    def test_status_empty(self):
        pool = CredentialPool("test")
        s = pool.status()
        assert s.total_keys == 0
        assert s.active_keys == 0
        assert not s.has_capacity

    def test_status_mixed(self):
        pool = CredentialPool("test")
        pool.add(_make_entry("active"))
        pool.add(_make_entry("exhausted", remaining_quota=0))
        pool.add(_make_entry("cooling", cooldown_until_ts=time.time() + 3600))
        s = pool.status()
        assert s.total_keys == 3
        assert s.active_keys == 1
        assert s.exhausted_keys == 1
        assert s.cooling_down_keys == 1
        assert s.has_capacity

    def test_status_unlimited_quota(self):
        pool = CredentialPool("test")
        pool.add(_make_entry(remaining_quota=-1))
        s = pool.status()
        assert s.aggregate_remaining_quota == -1


class TestPersistence:
    def test_persist_and_reload(self, tmp_path: Path):
        pool = CredentialPool("test", persist_dir=tmp_path)
        pool.add(_make_entry("a", provider="openai", remaining_quota=100))
        pool.add(_make_entry("b", provider="anthropic"))
        pool.acquire(provider="openai")

        pool2 = CredentialPool("test", persist_dir=tmp_path)
        assert sorted(pool2.keys()) == ["a", "b"]
        a = pool2.get("a")
        assert a.usage_count == 1
        assert a.remaining_quota == 100

    def test_persist_file_exists(self, tmp_path: Path):
        pool = CredentialPool("test", persist_dir=tmp_path)
        pool.add(_make_entry())
        assert (tmp_path / "test.json").exists()

    def test_corrupt_file_ignored(self, tmp_path: Path):
        (tmp_path / "test.json").write_text("not json")
        pool = CredentialPool("test", persist_dir=tmp_path)
        assert pool.keys() == []

    def test_wrong_schema_version_ignored(self, tmp_path: Path):
        data = {"schema_version": 9999, "entries": []}
        (tmp_path / "test.json").write_text(json.dumps(data))
        pool = CredentialPool("test", persist_dir=tmp_path)
        assert pool.keys() == []

    def test_snapshot(self):
        pool = CredentialPool("test")
        pool.add(_make_entry())
        snap = pool.snapshot()
        assert snap.pool_name == "test"
        assert len(snap.entries) == 1
        assert snap.schema_version == 1



class TestBatchAdd:
    def test_batch_add_multiple(self):
        pool = CredentialPool("test")
        entries = [_make_entry(f"k{i}") for i in range(5)]
        added = pool.batch_add(entries)
        assert added == 5
        assert len(pool.keys()) == 5

    def test_batch_add_skips_duplicates(self):
        pool = CredentialPool("test")
        pool.add(_make_entry("k0"))
        entries = [_make_entry("k0"), _make_entry("k1")]
        added = pool.batch_add(entries)
        assert added == 1
        assert sorted(pool.keys()) == ["k0", "k1"]

    def test_batch_add_empty(self):
        pool = CredentialPool("test")
        assert pool.batch_add([]) == 0


class TestExitProxyAndFingerprint:
    def test_entry_with_proxy(self):
        e = _make_entry(exit_proxy="socks5://10.0.0.1:1080")
        assert e.exit_proxy == "socks5://10.0.0.1:1080"
        assert e.is_available

    def test_entry_with_fingerprint(self):
        e = _make_entry(fingerprint_id="fp-alpha")
        assert e.fingerprint_id == "fp-alpha"

    def test_persist_proxy_and_fingerprint(self, tmp_path: Path):
        pool = CredentialPool("test", persist_dir=tmp_path)
        pool.add(_make_entry("k1", exit_proxy="http://proxy:8080", fingerprint_id="fp-1"))
        pool2 = CredentialPool("test", persist_dir=tmp_path)
        e = pool2.get("k1")
        assert e.exit_proxy == "http://proxy:8080"
        assert e.fingerprint_id == "fp-1"


class TestAvailableByProxy:
    def test_groups_by_proxy(self):
        pool = CredentialPool("test")
        pool.add(_make_entry("k1", exit_proxy="socks5://a:1080"))
        pool.add(_make_entry("k2", exit_proxy="socks5://a:1080"))
        pool.add(_make_entry("k3", exit_proxy="socks5://b:1080"))
        pool.add(_make_entry("k4"))
        result = pool.available_by_proxy()
        assert sorted(result["socks5://a:1080"]) == ["k1", "k2"]
        assert result["socks5://b:1080"] == ["k3"]
        assert result["direct"] == ["k4"]

    def test_excludes_unavailable(self):
        pool = CredentialPool("test")
        pool.add(_make_entry("k1", exit_proxy="socks5://a:1080"))
        pool.add(_make_entry("k2", exit_proxy="socks5://a:1080", remaining_quota=0))
        result = pool.available_by_proxy()
        assert result["socks5://a:1080"] == ["k1"]


class TestFromManifest:
    def test_load_manifest(self, tmp_path: Path):
        import json as _json
        manifest = tmp_path / "free-tier.json"
        manifest.write_text(_json.dumps({
            "pool_name": "free-tier",
            "cooldown_seconds": 90,
            "credentials": [
                {
                    "key_id": "oai-001",
                    "provider": "openai",
                    "secret_ref": "vault:oai-001",
                    "exit_proxy": "socks5://10.0.0.1:1080",
                    "fingerprint_id": "fp-alpha",
                    "remaining_quota": 200,
                },
                {
                    "key_id": "oai-002",
                    "provider": "openai",
                    "secret_ref": "vault:oai-002",
                },
            ],
        }))
        pool = CredentialPool.from_manifest(manifest)
        assert pool.name == "free-tier"
        assert len(pool.keys()) == 2
        e = pool.get("oai-001")
        assert e.exit_proxy == "socks5://10.0.0.1:1080"
        assert e.remaining_quota == 200


class TestReachabilityCheck:
    def test_all_reachable_by_default(self):
        pool = CredentialPool("test")
        pool.add(_make_entry("k1"))
        pool.add(_make_entry("k2"))
        result = pool.reachability_check()
        assert result == {"k1": True, "k2": True}

    def test_unreachable_gets_cooldown(self):
        pool = CredentialPool("test", cooldown_seconds=300)
        pool.add(_make_entry("k1"))
        pool.add(_make_entry("k2"))
        result = pool.reachability_check(probe_fn=lambda e: e.key_id != "k2")
        assert result == {"k1": True, "k2": False}
        assert pool.get("k2").cooldown_until_ts > time.time()

    def test_probe_exception_is_unreachable(self):
        pool = CredentialPool("test")
        pool.add(_make_entry("k1"))

        def bad_probe(e):
            raise ConnectionError("timeout")

        result = pool.reachability_check(probe_fn=bad_probe)
        assert result == {"k1": False}


class TestRotateExhausted:
    def test_removes_exhausted(self):
        pool = CredentialPool("test")
        pool.add(_make_entry("alive", remaining_quota=100))
        pool.add(_make_entry("dead1", remaining_quota=0))
        pool.add(_make_entry("dead2", remaining_quota=0))
        removed = pool.rotate_exhausted()
        assert len(removed) == 2
        assert sorted(e.key_id for e in removed) == ["dead1", "dead2"]
        assert pool.keys() == ["alive"]

    def test_noop_when_none_exhausted(self):
        pool = CredentialPool("test")
        pool.add(_make_entry("k1"))
        removed = pool.rotate_exhausted()
        assert removed == []
