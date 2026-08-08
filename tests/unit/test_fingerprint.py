"""Unit tests for fingerprint memoization.

Critical regression target: changing a memoized function's body MUST
change the fingerprint, so callers cannot serve stale outputs after a
bug fix.
"""

from __future__ import annotations

import contextlib
import importlib
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from bernstein.core.persistence.fingerprint import (
    _SOURCE_DIGEST_CACHE,
    MemoStore,
    code_digest,
    default_store,
    fingerprint,
    memoize_persistent,
)

if TYPE_CHECKING:
    from collections.abc import Generator
    from types import ModuleType

_PROBE_MODULE = "memo_dependency_probe"
#: Fixed base mtime so probe revisions get distinct, deterministic stamps.
_BASE_MTIME = 1_600_000_000


def _fn_v1(x: int, y: int) -> int:
    return x + y


def _fn_v2(x: int, y: int) -> int:
    # same signature, different body - fingerprint MUST diverge
    return x + y + 1


class TestFingerprintCore:
    def test_same_fn_same_args_same_key(self) -> None:
        assert fingerprint(_fn_v1, 1, 2) == fingerprint(_fn_v1, 1, 2)

    def test_same_fn_different_args_different_key(self) -> None:
        assert fingerprint(_fn_v1, 1, 2) != fingerprint(_fn_v1, 1, 3)

    def test_changed_function_body_changes_key(self) -> None:
        """Regression: this is the whole point of the work."""
        assert fingerprint(_fn_v1, 1, 2) != fingerprint(_fn_v2, 1, 2)

    def test_kwargs_order_does_not_matter(self) -> None:
        def f(*, a: int, b: int) -> int:
            return a + b

        assert fingerprint(f, a=1, b=2) == fingerprint(f, b=2, a=1)

    def test_digest_is_32_bytes(self) -> None:
        assert len(fingerprint(_fn_v1, 1, 2)) == 32

    def test_unhashable_args_fall_back_gracefully(self) -> None:
        digest = fingerprint(_fn_v1, {"complex": [1, 2, 3]})
        assert isinstance(digest, bytes)
        assert len(digest) == 32

    def test_two_distinct_fns_with_same_body_differ_by_qualname(self) -> None:
        def alpha(x: int) -> int:
            return x

        def beta(x: int) -> int:
            return x

        assert fingerprint(alpha, 1) != fingerprint(beta, 1)


class TestMemoStore:
    def test_get_miss_then_put_then_hit(self, tmp_path: Path) -> None:
        store = MemoStore(root=tmp_path / "memo", max_mb=1)
        digest = b"\x00" * 32
        assert store.get(digest) is None
        store.put(digest, {"answer": 42})
        assert store.get(digest) == {"answer": 42}

    def test_eviction_caps_total_size(self, tmp_path: Path) -> None:
        store = MemoStore(root=tmp_path / "memo", max_mb=0)
        store._max_bytes = 1024  # 1 KiB cap for test speed
        for i in range(50):
            store.put(bytes([i]) * 32, b"x" * 200)
        assert store.total_bytes() <= store._max_bytes

    def test_stats_track_hits_and_misses(self, tmp_path: Path) -> None:
        store = MemoStore(root=tmp_path / "memo", max_mb=1)
        digest = b"\x01" * 32
        assert store.get(digest) is None
        store.put(digest, "v")
        assert store.get(digest) == "v"
        stats = store.stats()
        assert stats.hits == 1
        assert stats.misses == 1


class TestMemoizePersistent:
    def test_decorator_caches_result(self, tmp_path: Path) -> None:
        store = MemoStore(root=tmp_path / "memo", max_mb=1)
        calls = {"n": 0}

        @memoize_persistent(store, site="test")
        def expensive(x: int) -> int:
            calls["n"] += 1
            return x * 2

        assert expensive(7) == 14
        assert expensive(7) == 14
        assert calls["n"] == 1

    def test_decorator_recomputes_when_inputs_change(self, tmp_path: Path) -> None:
        store = MemoStore(root=tmp_path / "memo", max_mb=1)
        calls = {"n": 0}

        @memoize_persistent(store, site="test")
        def expensive(x: int) -> int:
            calls["n"] += 1
            return x * 2

        expensive(1)
        expensive(2)
        expensive(3)
        assert calls["n"] == 3

    def test_default_store_uses_sdd_runtime_memo(self, tmp_path: Path) -> None:
        store = default_store(tmp_path)
        assert store.root == tmp_path / ".sdd" / "runtime" / "memo"


class TestDependencyAwareMemoization:
    """Memo keys must track the code a memoised shim *delegates to*.

    A one-line shim's own body is unchanged by any rewrite of the helper
    module it calls, so the function-body component of the fingerprint
    cannot see the edit and the store keeps serving output from the old
    helper.  ``depends_on`` closes that gap.
    """

    @pytest.fixture(autouse=True)
    def _isolate_probe_module(self, tmp_path: Path) -> Generator[None]:
        sys.path.insert(0, str(tmp_path))
        preserved = dict(_SOURCE_DIGEST_CACHE)
        # Two probe revisions can share a byte count and an mtime second, in
        # which case a written .pyc looks current and ``reload`` serves the
        # previous bytecode - masking what this class is here to test.
        bytecode_setting = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            yield
        finally:
            sys.dont_write_bytecode = bytecode_setting
            sys.modules.pop(_PROBE_MODULE, None)
            with contextlib.suppress(ValueError):
                sys.path.remove(str(tmp_path))
            _SOURCE_DIGEST_CACHE.clear()
            _SOURCE_DIGEST_CACHE.update(preserved)

    @staticmethod
    def _install_probe(tmp_path: Path, body: str, *, revision: int = 1) -> ModuleType:
        """Write *body* to the probe module and (re)import it.

        The digest cache is deliberately *not* cleared: these tests are
        about an interpreter that keeps running across the rewrite.
        ``revision`` stamps a distinct mtime so two same-length bodies
        cannot look unchanged to a stat-based check.
        """
        path = tmp_path / f"{_PROBE_MODULE}.py"
        path.write_text(body, encoding="utf-8")
        stamp = _BASE_MTIME + revision
        os.utime(path, (stamp, stamp))
        existing = sys.modules.get(_PROBE_MODULE)
        if existing is not None:
            return importlib.reload(existing)
        return importlib.import_module(_PROBE_MODULE)

    def test_code_digest_tracks_module_source(self, tmp_path: Path) -> None:
        first = code_digest(self._install_probe(tmp_path, 'MARKER = "v1"\n', revision=1))
        second = code_digest(self._install_probe(tmp_path, 'MARKER = "v2"\n', revision=2))
        assert first != second

    def test_code_digest_recomputes_after_in_process_module_reload(self, tmp_path: Path) -> None:
        """A long-running process must not pin the digest it first saw.

        ``plugin_hotreload`` swaps module code via ``importlib.reload``
        without restarting, so a name-keyed digest cache would keep
        folding the pre-reload source into every subsequent memo key.
        """
        module = self._install_probe(tmp_path, 'MARKER = "v1"\n', revision=1)
        before = code_digest(module)

        reloaded = self._install_probe(tmp_path, 'MARKER = "v2"\n', revision=2)

        assert reloaded.MARKER == "v2", "probe did not actually reload"
        assert code_digest(reloaded) != before

    def test_code_digest_is_stable_for_unchanged_source(self, tmp_path: Path) -> None:
        module = self._install_probe(tmp_path, 'MARKER = "v1"\n')
        assert code_digest(module) == code_digest(module)

    def test_code_digest_tracks_callable_body(self) -> None:
        assert code_digest(_fn_v1) != code_digest(_fn_v2)

    def test_shim_recomputes_when_dependency_module_changes(self, tmp_path: Path) -> None:
        """The regression: identical args and identical shim, new helper."""
        store = MemoStore(root=tmp_path / "memo", max_mb=1)

        def build() -> Any:
            module = sys.modules[_PROBE_MODULE]

            @memoize_persistent(store, site="probe", depends_on=(module,))
            def shim(*, name: str) -> str:
                return str(sys.modules[_PROBE_MODULE].extract(name))

            return shim

        self._install_probe(tmp_path, 'def extract(name):\n    return "v1:" + name\n', revision=1)
        assert build()(name="a") == "v1:a"
        # Nothing changed - must be a cache hit, not a recompute.
        assert build()(name="a") == "v1:a"

        self._install_probe(tmp_path, 'def extract(name):\n    return "v2:" + name\n', revision=2)
        assert build()(name="a") == "v2:a"

    def test_absent_depends_on_leaves_existing_keys_untouched(self, tmp_path: Path) -> None:
        """Existing memo sites must not be invalidated wholesale."""
        store = MemoStore(root=tmp_path / "memo", max_mb=1)

        @memoize_persistent(store, site="plain")
        def plain(x: int) -> int:
            return x * 2

        assert fingerprint(plain, 3) == fingerprint(plain.__wrapped__, 3)

    def test_declared_dependencies_are_introspectable(self, tmp_path: Path) -> None:
        store = MemoStore(root=tmp_path / "memo", max_mb=1)
        module = self._install_probe(tmp_path, "MARKER = 1\n")

        @memoize_persistent(store, site="probe", depends_on=(module,))
        def shim(x: int) -> int:
            return x

        assert shim.__memo_depends_on__ == (module,)


class TestPerfStress:
    """1000-entry stress test to confirm size cap holds under load."""

    @pytest.mark.parametrize("n_entries", [1000])
    def test_eviction_holds_under_1000_entries(self, tmp_path: Path, n_entries: int) -> None:
        store = MemoStore(root=tmp_path / "memo", max_mb=1)
        store._max_bytes = 64 * 1024  # 64 KiB cap
        payload = b"y" * 256
        for i in range(n_entries):
            digest = i.to_bytes(4, "big") + b"\x00" * 28
            store.put(digest, payload)
        assert store.total_bytes() <= store._max_bytes
