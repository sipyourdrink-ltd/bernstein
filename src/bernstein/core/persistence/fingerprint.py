"""Content-addressed fingerprint memoization for expensive recomputes.

Borrows the *invalidation rule* from cocoindex's memo fingerprint:
key = ``hash(canonicalized_input) XOR hash(canonicalized_code_AST)``.

The point of folding the function body into the key is to invalidate
cached entries whenever the function that produced them changes.  Plain
``hash(input)`` keys (as used by most of Bernstein's ad-hoc caches)
silently keep stale outputs after the producer is rewritten.

The body component only covers the memoised function's *own* source.  A
thin shim that delegates to a helper module keeps a byte-identical body
when that helper is rewritten, so its cached entries survive the fix.
Such call sites must name the code they delegate to via
``memoize_persistent(..., depends_on=...)``; see :func:`code_digest`.

Prior art: ``cocoindex/python/cocoindex/_internal/memo_fingerprint.py``
(https://github.com/cocoindex-io/cocoindex/blob/main/python/cocoindex/_internal/memo_fingerprint.py).
That implementation is ~400 LOC of Apache-2.0 Python+Rust tightly
coupled to their ``@coco.fn`` decorator; we borrow only the idea, not
the dependency.

Storage layout::

    .sdd/runtime/memo/<sha-prefix>/<sha-suffix>.bin

Eviction is approximate LRU sized by ``defaults.JANITOR.memo_max_mb``.
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import inspect
import json
import logging
import operator
import os
import pickle
import textwrap
import threading
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, cast

logger = logging.getLogger(__name__)

_DIGEST_BYTES = 32
_DEFAULT_MAX_MB = 200
_AST_CACHE: dict[str, bytes] = {}
_AST_CACHE_LOCK = threading.Lock()
#: Source digests for :func:`code_digest` targets, keyed by ``__name__``
#: for modules and ``<module>.<qualname>`` for callables.  The value
#: carries the ``(mtime_ns, size)`` the digest was computed from, so a
#: module rewritten under a running interpreter is re-hashed rather than
#: served from the name alone; ``None`` marks a target with no statable
#: file.
_SOURCE_DIGEST_CACHE: dict[str, tuple[tuple[int, int] | None, bytes]] = {}

#: A callable or module whose source participates in a memo key.
CodeDependency = Callable[..., Any] | ModuleType


def _canonicalize(value: Any) -> Any:
    """Recursively normalise *value* into a JSON-friendly, order-stable form.

    Sets and frozensets are sorted (by ``repr`` of their members, which
    is total even for mixed-type members) so two processes with
    different ``PYTHONHASHSEED`` produce identical bytes.  Without this,
    ``json.dumps({"a","b"}, default=repr)`` round-trips through
    ``repr(set)`` whose member order is hash-randomised - yielding a
    different cache key per process and silently breaking action-cache
    hits in CI.  Dicts recurse so nested sets are also canonicalised.
    """
    if isinstance(value, dict):
        items = cast("Iterable[tuple[Any, Any]]", value.items())
        return {str(k): _canonicalize(v) for k, v in sorted(items, key=lambda kv: repr(kv[0]))}
    if isinstance(value, (set, frozenset)):
        values = cast("Iterable[Any]", value)
        return ["__set__", sorted((_canonicalize(v) for v in values), key=repr)]
    if isinstance(value, (list, tuple)):
        values = cast("Iterable[Any]", value)
        return [_canonicalize(v) for v in values]
    return value


def _canonical_arg_repr(value: Any) -> bytes:
    """Return a stable byte representation of an argument value.

    Canonicalises sets/frozensets and nested containers before JSON
    encoding so member order is fixed across processes (Python's hash
    randomisation otherwise yields different bytes per
    ``PYTHONHASHSEED``).  Falls back to pickle for objects JSON cannot
    encode; the fall-back path is reached only by exotic types (custom
    classes, file objects, etc.) that callers should not normally pass
    through a memoised function anyway.
    """
    try:
        return json.dumps(_canonicalize(value), sort_keys=True, default=repr).encode("utf-8")
    except (TypeError, ValueError):
        try:
            return pickle.dumps(value, protocol=5)
        except (pickle.PicklingError, TypeError):
            return repr(value).encode("utf-8", errors="replace")


def _function_ast_bytes(fn: Callable[..., Any]) -> bytes:
    """Return canonical bytes describing the function's body.

    Strategy: dedented source text of the function (or its ``__wrapped__``
    target if the callable has been decorated).  Falls back to the qualified
    name when source is unavailable (e.g. C extensions, REPL-defined fns).
    """
    target = inspect.unwrap(fn)
    qualname = getattr(target, "__qualname__", repr(target))
    module = getattr(target, "__module__", "?")
    cache_key = f"{module}.{qualname}"
    cached = _AST_CACHE.get(cache_key)
    if cached is not None:
        return cached

    try:
        source = inspect.getsource(target)
        body = textwrap.dedent(source).encode("utf-8")
    except (OSError, TypeError):
        body = cache_key.encode("utf-8")

    with _AST_CACHE_LOCK:
        _AST_CACHE[cache_key] = body
    return body


def _source_cache_key(target: CodeDependency) -> str:
    """Return the stable per-process cache key for *target*.

    Modules key on ``__name__`` rather than ``repr`` so the key does not
    embed an install path (which differs per machine and would defeat
    the cache on every process).
    """
    if isinstance(target, ModuleType):
        return getattr(target, "__name__", repr(target))
    inner = inspect.unwrap(target)
    module = getattr(inner, "__module__", "?")
    qualname = getattr(inner, "__qualname__", repr(inner))
    return f"{module}.{qualname}"


def _module_source_bytes(module: ModuleType) -> bytes:
    """Return the on-disk bytes of *module*, or its name when unreadable.

    Reads the file directly rather than going through ``inspect``: the
    latter serves from ``linecache``, which can hand back a previous
    revision of a file rewritten within the same mtime granularity.
    """
    path = getattr(module, "__file__", None)
    if path:
        try:
            return Path(path).read_bytes()
        except OSError:
            pass
    try:
        return inspect.getsource(module).encode("utf-8")
    except (OSError, TypeError):
        return _source_cache_key(module).encode("utf-8")


def _module_stat_token(module: ModuleType) -> tuple[int, int] | None:
    """Return ``(mtime_ns, size)`` for *module*'s file, or ``None``.

    ``None`` means the module has no statable file (namespace package,
    frozen or zipped import), in which case the digest falls back to
    being computed once per process.
    """
    path = getattr(module, "__file__", None)
    if not path:
        return None
    try:
        stat = Path(path).stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def code_digest(*targets: CodeDependency) -> bytes:
    """Return a 32-byte digest over the source of every target.

    Callables hash via their dedented source; modules hash via their
    whole on-disk bytes.  Module granularity is deliberate - a memoised
    value produced by ``mod.f`` is also invalidated by an edit to a
    private helper ``f`` calls, or to a dataclass the value is an
    instance of, neither of which a per-callable hash can see.  The cost
    is over-invalidation (an unrelated edit to the module rebuilds the
    cache), which is the safe direction.

    Module digests are cached against the file's ``(mtime_ns, size)`` and
    re-derived when that moves, so a module swapped in by
    ``importlib.reload`` under a long-running process (see
    ``plugins_core.plugin_hotreload``) does not keep folding its
    pre-reload source into new keys.  Callable targets inherit
    :func:`fingerprint`'s per-process source cache; prefer passing the
    owning module when a target may be reloaded.
    """
    hasher = hashlib.sha256()
    for target in targets:
        key = _source_cache_key(target)
        token = _module_stat_token(target) if isinstance(target, ModuleType) else None
        entry = _SOURCE_DIGEST_CACHE.get(key)
        if entry is None or entry[0] != token:
            body = _module_source_bytes(target) if isinstance(target, ModuleType) else _function_ast_bytes(target)
            entry = (token, hashlib.sha256(body).digest())
            with _AST_CACHE_LOCK:
                _SOURCE_DIGEST_CACHE[key] = entry
        hasher.update(key.encode("utf-8") + b"\0" + entry[1])
    return hasher.digest()


def fingerprint(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> bytes:
    """Compute a 32-byte digest of (qualname, function body, args).

    Two different functions with identical bodies will still differ via
    qualname; the same function with a changed body will differ via the
    AST/source bytes.  Argument order is preserved; kwargs are sorted.
    """
    target = inspect.unwrap(fn)
    qualname = getattr(target, "__qualname__", repr(target))
    code_bytes = _function_ast_bytes(fn)

    args_blob = b"|".join(_canonical_arg_repr(a) for a in args)
    kwargs_blob = b"|".join(f"{k}=".encode() + _canonical_arg_repr(v) for k, v in sorted(kwargs.items()))
    input_digest = hashlib.sha256(qualname.encode("utf-8") + b"\0" + args_blob + b"\0" + kwargs_blob).digest()
    code_digest = hashlib.sha256(code_bytes).digest()
    return bytes(a ^ b for a, b in zip(input_digest, code_digest, strict=True))


@dataclass(frozen=True)
class MemoStats:
    """Per-process counters surfaced to Prometheus."""

    hits: int = 0
    misses: int = 0
    bytes_used: int = 0


class MemoStore:
    """Disk-backed content-addressed memo store with size-based LRU.

    Files live at ``root/<aa>/<rest>.bin`` where ``aa`` is the first
    byte of the digest hex.  Atime-based eviction keeps total size below
    ``max_mb`` MiB.
    """

    def __init__(self, root: Path, max_mb: int = _DEFAULT_MAX_MB) -> None:
        self._root = root
        self._max_bytes = max_mb * 1024 * 1024
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    @property
    def root(self) -> Path:
        return self._root

    def _path_for(self, digest: bytes) -> Path:
        hexd = digest.hex()
        return self._root / hexd[:2] / f"{hexd[2:]}.bin"

    def get(self, digest: bytes) -> Any | None:
        """Return cached value for *digest* or ``None`` on miss.

        Corrupt entries (truncated, partial-write, wrong protocol) are
        unlinked before returning ``None`` so the next ``put`` can
        replace them; otherwise a one-time corruption would force an
        infinite stream of misses on a hot key.
        """
        path = self._path_for(digest)
        if not path.exists():
            with self._lock:
                self._misses += 1
            return None
        try:
            data = path.read_bytes()
            value = pickle.loads(data)
        except (OSError, pickle.UnpicklingError, EOFError, ValueError) as exc:
            logger.debug("memo: failed to load %s: %s", path, exc)
            # Self-heal: drop the bad entry so the next put repopulates it.
            with contextlib.suppress(OSError):
                path.unlink()
            with self._lock:
                self._misses += 1
            return None
        # Refresh atime so eviction prefers older entries.
        with contextlib.suppress(OSError):
            path.touch()
        with self._lock:
            self._hits += 1
        return value

    def put(self, digest: bytes, value: Any) -> None:
        """Persist *value* under *digest*; evict if size cap exceeded.

        Concurrent writers targeting the same digest are tolerated by
        giving each writer a unique temp filename
        (``<name>.<pid>.<rand>.tmp``) and using ``os.replace`` for the
        atomic rename onto the final path.  Without per-writer temp
        paths two threads sharing one ``MemoStore`` instance race: the
        first ``replace`` unlinks the temp, the second writer's
        subsequent ``replace`` then sees ``FileNotFoundError``.
        """
        path = self._path_for(digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            payload = pickle.dumps(value, protocol=5)
        except (pickle.PicklingError, TypeError) as exc:
            logger.debug("memo: cannot pickle value for %s: %s", digest.hex()[:12], exc)
            return
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_bytes(payload)
            os.replace(tmp, path)
        except OSError as exc:
            # Best-effort cleanup; another concurrent writer may have already
            # claimed the slot which is fine - content is content-addressed.
            logger.debug("memo: put failed for %s: %s", digest.hex()[:12], exc)
            with contextlib.suppress(OSError):
                tmp.unlink()
            return
        self._maybe_evict()

    def _iter_entries(self) -> list[tuple[Path, int, float]]:
        if not self._root.exists():
            return []
        out: list[tuple[Path, int, float]] = []
        for sub in self._root.iterdir():
            if not sub.is_dir():
                continue
            for entry in sub.iterdir():
                if entry.suffix != ".bin":
                    continue
                try:
                    st = entry.stat()
                except OSError:
                    continue
                out.append((entry, st.st_size, st.st_atime))
        return out

    def total_bytes(self) -> int:
        return sum(size for _, size, _ in self._iter_entries())

    def _maybe_evict(self) -> None:
        entries = self._iter_entries()
        total = sum(size for _, size, _ in entries)
        if total <= self._max_bytes:
            return
        entries.sort(key=operator.itemgetter(2))  # oldest atime first
        for path, size, _ in entries:
            if total <= self._max_bytes:
                break
            with contextlib.suppress(OSError):
                path.unlink()
                total -= size

    def stats(self) -> MemoStats:
        with self._lock:
            return MemoStats(hits=self._hits, misses=self._misses, bytes_used=self.total_bytes())


def memoize_persistent[F: Callable[..., Any]](
    store: MemoStore,
    *,
    site: str = "default",
    depends_on: Sequence[CodeDependency] = (),
) -> Callable[[F], F]:
    """Decorator that caches function results in *store* keyed by fingerprint.

    Use *site* as a stable label for metrics (e.g.
    ``"cross_model_verifier"``).  The label ensures Prometheus counters
    can be partitioned per call-site without forcing a global registry.

    Pass *depends_on* whenever the decorated function is a shim over code
    living elsewhere: the fingerprint sees only the decorated body, so a
    rewrite of the delegate would otherwise leave every cached entry in
    place.  Each entry is a callable or - preferably - the module that
    owns the real work; see :func:`code_digest`.  Leaving it empty keeps
    keys byte-identical to the pre-``depends_on`` scheme, so existing
    stores are not invalidated wholesale.
    """
    dependencies = tuple(depends_on)

    def _key(target: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]) -> bytes:
        base = fingerprint(target, *args, **kwargs)
        if not dependencies:
            return base
        return bytes(a ^ b for a, b in zip(base, code_digest(*dependencies), strict=True))

    def decorator(target: F) -> F:
        if inspect.iscoroutinefunction(target):

            @functools.wraps(target)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                digest = _key(target, args, kwargs)
                cached = store.get(digest)
                if cached is not None:
                    _record_metric("hit", site)
                    return cached
                value = await target(*args, **kwargs)
                store.put(digest, value)
                _record_metric("miss", site)
                return value

            async_wrapper.__memo_store__ = store  # type: ignore[attr-defined]
            async_wrapper.__memo_site__ = site  # type: ignore[attr-defined]
            async_wrapper.__memo_depends_on__ = dependencies  # type: ignore[attr-defined]
            return cast("F", async_wrapper)

        @functools.wraps(target)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            digest = _key(target, args, kwargs)
            cached = store.get(digest)
            if cached is not None:
                _record_metric("hit", site)
                return cached
            value = target(*args, **kwargs)
            store.put(digest, value)
            _record_metric("miss", site)
            return value

        wrapper.__memo_store__ = store  # type: ignore[attr-defined]
        wrapper.__memo_site__ = site  # type: ignore[attr-defined]
        wrapper.__memo_depends_on__ = dependencies  # type: ignore[attr-defined]
        return cast("F", wrapper)

    return decorator


def _record_metric(kind: str, site: str) -> None:
    """Best-effort Prometheus counter increment.  No-op when unavailable."""
    try:
        from bernstein.core.observability import prometheus as _p
    except ImportError:
        return
    counter = getattr(_p, "memo_hits_total" if kind == "hit" else "memo_misses_total", None)
    if counter is None:
        return
    with contextlib.suppress(Exception):
        counter.labels(site=site).inc()


def default_store(workdir: Path, max_mb: int | None = None) -> MemoStore:
    """Return the canonical store rooted at ``<workdir>/.sdd/runtime/memo``.

    Honours ``defaults.JANITOR.memo_max_mb`` when *max_mb* is not given.
    """
    if max_mb is None:
        try:
            from bernstein.core import defaults as _defaults

            max_mb = int(getattr(_defaults.JANITOR, "memo_max_mb", _DEFAULT_MAX_MB))
        except (ImportError, AttributeError):
            max_mb = _DEFAULT_MAX_MB
    return MemoStore(root=workdir / ".sdd" / "runtime" / "memo", max_mb=max_mb)


__all__ = [
    "CodeDependency",
    "MemoStats",
    "MemoStore",
    "code_digest",
    "default_store",
    "fingerprint",
    "memoize_persistent",
]
