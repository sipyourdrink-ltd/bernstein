"""Content-addressed local trace store + read-only viewer support.

This module sits next to the existing ``traces.py`` emitter and indexer.
Where ``traces.py`` writes per-task JSONL files for the live orchestrator,
``trace_store.py`` archives finalised traces as content-addressed blobs so
that operators can run a local read-only viewer over them without paying
for a hosted trace platform.

The layout under the configured traces directory (default ``.sdd/traces/``)
is::

    blobs/<sha256[:2]>/<sha256>.jsonl.<ext>   # immutable trace blobs
    index.jsonl                                # one TraceIndexEntry per line

``<ext>`` is ``zst`` when the optional ``zstandard`` package is importable;
otherwise we fall back to ``gz`` (stdlib ``gzip``). The codec used for a
given blob is recorded in its index entry, so the store can read either
format regardless of which codec wrote it.

Design notes
------------

* **Append-only.** ``put`` is idempotent: writing the same bytes a second
  time is a no-op and returns the existing sha256.
* **Cheap to verify.** ``verify`` rereads the blob, decompresses it, and
  rehashes; the index does not have to be trusted.
* **Cheap to rebuild.** ``reindex`` discards ``index.jsonl`` and walks the
  ``blobs/`` tree, regenerating the index from the on-disk bytes.
* **No new heavy deps.** FastAPI + uvicorn are already in ``pyproject.toml``;
  zstandard is optional; the HTML viewer is a single inline template with
  HTMX-friendly endpoints.

The public surface is intentionally small:

* :class:`ContentAddressedTraceStore` - ``put``, ``get``, ``verify``,
  ``reindex``, ``index``.
* :class:`TraceIndexEntry` - one row in ``index.jsonl``.
* :func:`build_viewer_app` - FastAPI factory used by ``bernstein trace serve``.

See ``docs/observability/trace-store.md`` for the operator-facing guide.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, Final

from bernstein.core.observability.log_redact import redact_sensitive_bytes
from bernstein.core.persistence.cas_store import CASIntegrityError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BLOBS_DIRNAME: Final = "blobs"
_INDEX_FILENAME: Final = "index.jsonl"
_GZIP_EXT: Final = "gz"
_ZSTD_EXT: Final = "zst"
_CODEC_GZIP: Final = "gzip"
_CODEC_ZSTD: Final = "zstd"


def _zstd_available() -> bool:
    """Return True if the optional ``zstandard`` codec can be imported."""
    try:  # pragma: no cover - import shape check
        import zstandard  # noqa: F401
    except ImportError:
        return False
    return True


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TraceIndexEntry:
    """One row in ``index.jsonl``.

    The index is intentionally narrow: it carries just enough metadata to
    list, search, and de-duplicate traces in the viewer. Detailed trace
    bodies are read on-demand from the content-addressed blob.

    Attributes:
        trace_id: Logical trace identifier (matches ``AgentTrace.trace_id``
            when the source is the live emitter; otherwise any unique
            string the caller chose).
        task_id: Task identifier the trace belongs to. Empty string if the
            trace is not attached to a task.
        sha256: Hex digest of the uncompressed trace bytes. Doubles as the
            blob lookup key.
        byte_size: Size of the uncompressed trace bytes.
        started_at: Unix timestamp of the first event in the trace.
        ended_at: Unix timestamp of the last event in the trace; ``None``
            if the trace is still open.
        model: Optional model short name (e.g. ``"sonnet"``).
        cost_usd: Optional total USD cost recorded with the trace.
        codec: Compression codec used for the on-disk blob (``"zstd"`` or
            ``"gzip"``).
    """

    trace_id: str
    task_id: str
    sha256: str
    byte_size: int
    started_at: float
    ended_at: float | None = None
    model: str = ""
    cost_usd: float = 0.0
    codec: str = _CODEC_GZIP

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TraceIndexEntry:
        return cls(
            trace_id=str(d.get("trace_id", "")),
            task_id=str(d.get("task_id", "")),
            sha256=str(d.get("sha256", "")),
            byte_size=int(d.get("byte_size", 0)),
            started_at=float(d.get("started_at", 0.0)),
            ended_at=None if d.get("ended_at") is None else float(d["ended_at"]),
            model=str(d.get("model", "")),
            cost_usd=float(d.get("cost_usd", 0.0)),
            codec=str(d.get("codec", _CODEC_GZIP)),
        )


@dataclass
class TraceMetadataHints:
    """Optional metadata a caller can attach when storing a trace.

    The hints are not required for correctness: ``put`` will derive what it
    can from the trace bytes when a hint is missing. The hint takes
    precedence when supplied.
    """

    trace_id: str = ""
    task_id: str = ""
    started_at: float = 0.0
    ended_at: float | None = None
    model: str = ""
    cost_usd: float = 0.0


# ---------------------------------------------------------------------------
# Codec helpers
# ---------------------------------------------------------------------------


def _compress(data: bytes, codec: str) -> bytes:
    if codec == _CODEC_ZSTD:
        import zstandard  # local import; only reached when available

        return zstandard.ZstdCompressor().compress(data)
    return gzip.compress(data)


def _decompress(data: bytes, codec: str) -> bytes:
    if codec == _CODEC_ZSTD:
        import zstandard

        return zstandard.ZstdDecompressor().decompress(data)
    return gzip.decompress(data)


def _ext_for_codec(codec: str) -> str:
    return _ZSTD_EXT if codec == _CODEC_ZSTD else _GZIP_EXT


def _codec_for_ext(ext: str) -> str:
    return _CODEC_ZSTD if ext == _ZSTD_EXT else _CODEC_GZIP


# ---------------------------------------------------------------------------
# Metadata extraction
# ---------------------------------------------------------------------------


def _extract_hints_from_bytes(raw: bytes) -> TraceMetadataHints:
    """Best-effort metadata extraction from JSON or JSONL trace bytes.

    The function is lossy on purpose: when fields are missing we leave the
    defaults in place. Callers that need exact metadata should pass an
    explicit :class:`TraceMetadataHints`.
    """
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return TraceMetadataHints()

    # Try a single JSON object first (matches AgentTrace.write payload).
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        obj = None
    if isinstance(obj, dict):
        return _hints_from_obj(obj)

    # Fall back to JSONL: scan first/last non-empty lines.
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return TraceMetadataHints()

    first: dict[str, Any] = {}
    last: dict[str, Any] = {}
    try:
        first = json.loads(lines[0])
        if not isinstance(first, dict):
            first = {}
    except json.JSONDecodeError:
        first = {}
    try:
        last = json.loads(lines[-1])
        if not isinstance(last, dict):
            last = {}
    except json.JSONDecodeError:
        last = {}

    hints = _hints_from_obj(first)
    if last:
        end_ts = _coerce_float(last.get("end_ts") or last.get("timestamp") or last.get("ts"))
        if end_ts is not None:
            hints.ended_at = end_ts
        if not hints.model:
            hints.model = str(last.get("model", "") or "")
        if not hints.cost_usd:
            hints.cost_usd = _coerce_float(last.get("cost_usd")) or 0.0
    return hints


def _hints_from_obj(obj: dict[str, Any]) -> TraceMetadataHints:
    """Build hints from a single trace JSON object."""
    task_ids = obj.get("task_ids")
    task_id = ""
    if isinstance(task_ids, list) and task_ids:
        task_id = str(task_ids[0])
    elif isinstance(obj.get("task_id"), str):
        task_id = str(obj["task_id"])

    started = _coerce_float(obj.get("spawn_ts") or obj.get("started_at") or obj.get("ts") or obj.get("timestamp"))
    ended = _coerce_float(obj.get("end_ts") or obj.get("ended_at"))
    return TraceMetadataHints(
        trace_id=str(obj.get("trace_id", "") or ""),
        task_id=task_id,
        started_at=started if started is not None else 0.0,
        ended_at=ended,
        model=str(obj.get("model", "") or ""),
        cost_usd=_coerce_float(obj.get("cost_usd")) or 0.0,
    )


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class ContentAddressedTraceStore:
    """Content-addressed archive of agent trace bodies.

    The store is rooted at a directory (typically ``.sdd/traces/``) and
    keeps the on-disk layout described in the module docstring. It is
    safe to construct in a process that has no traces yet - directories
    are created lazily on first ``put`` / ``reindex`` call.

    Args:
        traces_dir: Path to the traces directory.
        prefer_zstd: If True and ``zstandard`` is importable, new blobs
            are written with zstd compression; otherwise gzip is used.
            Existing blobs are read with whatever codec their filename
            declares, regardless of this flag.
    """

    def __init__(self, traces_dir: Path, *, prefer_zstd: bool = True) -> None:
        self._dir = traces_dir
        self._prefer_zstd = prefer_zstd and _zstd_available()

    # -- Paths --------------------------------------------------------------

    @property
    def root(self) -> Path:
        """Filesystem root of the store."""
        return self._dir

    @property
    def blobs_dir(self) -> Path:
        return self._dir / _BLOBS_DIRNAME

    @property
    def index_path(self) -> Path:
        return self._dir / _INDEX_FILENAME

    @property
    def codec(self) -> str:
        """Codec used for newly written blobs."""
        return _CODEC_ZSTD if self._prefer_zstd else _CODEC_GZIP

    def _blob_path(self, sha256: str, codec: str) -> Path:
        if len(sha256) < 2:
            msg = "sha256 too short for content-addressed layout"
            raise ValueError(msg)
        return self.blobs_dir / sha256[:2] / f"{sha256}.jsonl.{_ext_for_codec(codec)}"

    def _existing_blob_path(self, sha256: str) -> Path | None:
        """Return the on-disk path of the blob with this digest, if any."""
        if len(sha256) < 2:
            return None
        bucket = self.blobs_dir / sha256[:2]
        if not bucket.exists():
            return None
        for ext in (_ZSTD_EXT, _GZIP_EXT):
            candidate = bucket / f"{sha256}.jsonl.{ext}"
            if candidate.exists():
                return candidate
        return None

    # -- Writes -------------------------------------------------------------

    def put(
        self,
        trace_bytes: bytes,
        *,
        hints: TraceMetadataHints | None = None,
    ) -> TraceIndexEntry:
        """Store ``trace_bytes`` and append/refresh its index entry.

        The operation is idempotent: writing the same bytes twice produces
        the same sha256, leaves the blob on disk unchanged, and refreshes
        the index entry in place (latest metadata wins).
        """
        if not isinstance(trace_bytes, (bytes, bytearray)):
            msg = "trace_bytes must be bytes"
            raise TypeError(msg)
        # Redaction precedes content addressing.  The digest identifies the
        # exact safe bytes stored and later verified; hashing first would make
        # a redacted blob fail its own receipt.
        incoming = bytes(trace_bytes) if isinstance(trace_bytes, bytearray) else trace_bytes
        raw = redact_sensitive_bytes(incoming)
        sha256 = hashlib.sha256(raw).hexdigest()

        existing = self._existing_blob_path(sha256)
        if existing is not None:
            codec = _codec_for_ext(existing.suffix.lstrip("."))
        else:
            codec = self.codec
            target = self._blob_path(sha256, codec)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(_compress(raw, codec))

        entry = self._build_entry(raw, sha256, codec, hints)
        self._upsert_index(entry)
        return entry

    def _build_entry(
        self,
        raw: bytes,
        sha256: str,
        codec: str,
        hints: TraceMetadataHints | None,
    ) -> TraceIndexEntry:
        derived = _extract_hints_from_bytes(raw)
        if hints is None:
            hints = TraceMetadataHints()

        trace_id = hints.trace_id or derived.trace_id or sha256[:16]
        task_id = hints.task_id or derived.task_id
        started_at = hints.started_at or derived.started_at
        ended_at = hints.ended_at if hints.ended_at is not None else derived.ended_at
        model = hints.model or derived.model
        cost_usd = hints.cost_usd or derived.cost_usd

        return TraceIndexEntry(
            trace_id=trace_id,
            task_id=task_id,
            sha256=sha256,
            byte_size=len(raw),
            started_at=started_at,
            ended_at=ended_at,
            model=model,
            cost_usd=cost_usd,
            codec=codec,
        )

    # -- Reads --------------------------------------------------------------

    def get(self, trace_id: str, *, verify: bool = True) -> bytes | None:
        """Return uncompressed trace bytes for ``trace_id``.

        ``trace_id`` can be either the logical trace identifier or the
        sha256 digest of the blob. Returns ``None`` if no matching entry
        exists in the index or the blob is missing on disk.

        By default the decompressed bytes are re-hashed and checked against
        the indexed ``sha256`` - the same property :meth:`verify` provides -
        so the documented "index does not have to be trusted" guarantee
        holds on the hot read path, not only when an operator calls
        :meth:`verify` explicitly. On a mismatch a
        :class:`~bernstein.core.persistence.cas_store.CASIntegrityError` is
        raised rather than decompressed-but-wrong bytes being returned.

        Args:
            trace_id: Logical trace id or sha256 digest of the blob.
            verify: When ``True`` (default), re-hash the decompressed bytes
                and raise on a mismatch with the indexed digest. Set
                ``False`` only for callers that have already verified the
                content upstream; the opt-out re-opens the integrity hole
                for that call.

        Returns:
            The uncompressed trace bytes, or ``None`` when the trace is
            unknown or its blob is missing.

        Raises:
            CASIntegrityError: If *verify* is ``True`` and the decompressed
                bytes do not hash to the indexed digest.
        """
        entry = self._lookup_entry(trace_id)
        if entry is None:
            return None
        path = self._existing_blob_path(entry.sha256)
        if path is None:
            return None
        codec = _codec_for_ext(path.suffix.lstrip("."))
        raw = _decompress(path.read_bytes(), codec)
        if verify:
            actual = hashlib.sha256(raw).hexdigest()
            if actual != entry.sha256:
                logger.error(
                    "trace_store integrity check failed: indexed %s, on-disk bytes hash to %s",
                    entry.sha256,
                    actual,
                )
                raise CASIntegrityError(expected=entry.sha256, actual=actual)
        return raw

    def index(self) -> list[TraceIndexEntry]:
        """Return all index entries, most recent ``started_at`` first."""
        entries = list(self._read_index())
        entries.sort(key=lambda e: e.started_at, reverse=True)
        return entries

    def verify(self, trace_id: str) -> bool:
        """Confirm the on-disk bytes match the indexed sha256.

        Returns ``False`` if the trace is unknown, the blob is missing,
        or the recomputed digest disagrees with the index entry.
        """
        entry = self._lookup_entry(trace_id)
        if entry is None:
            return False
        path = self._existing_blob_path(entry.sha256)
        if path is None:
            return False
        codec = _codec_for_ext(path.suffix.lstrip("."))
        try:
            raw = _decompress(path.read_bytes(), codec)
        except Exception:
            return False
        return hashlib.sha256(raw).hexdigest() == entry.sha256

    # -- Index maintenance --------------------------------------------------

    def reindex(self) -> int:
        """Rebuild ``index.jsonl`` by walking the blobs tree.

        Returns the number of entries written.
        """
        entries: list[TraceIndexEntry] = []
        if self.blobs_dir.exists():
            for blob_path in sorted(self.blobs_dir.rglob("*.jsonl.*")):
                if not blob_path.is_file():
                    continue
                sha256 = blob_path.name.split(".", 1)[0]
                codec = _codec_for_ext(blob_path.suffix.lstrip("."))
                try:
                    raw = _decompress(blob_path.read_bytes(), codec)
                except Exception:
                    logger.warning("trace_store: skipping unreadable blob %s", blob_path)
                    continue
                if hashlib.sha256(raw).hexdigest() != sha256:
                    logger.warning("trace_store: digest mismatch for %s", blob_path)
                    continue
                entries.append(self._build_entry(raw, sha256, codec, hints=None))

        entries.sort(key=lambda e: e.started_at, reverse=True)
        self._write_index(entries)
        return len(entries)

    # -- Search helpers (used by the viewer) -------------------------------

    def search(
        self,
        *,
        task_id: str = "",
        model: str = "",
        text: str = "",
    ) -> list[TraceIndexEntry]:
        """Return index entries matching the given filters.

        All filters are applied with case-insensitive substring matching.
        Empty filters match everything; ``text`` matches against
        ``trace_id``, ``task_id``, or ``sha256``.
        """
        task_needle = task_id.lower()
        model_needle = model.lower()
        text_needle = text.lower()

        results: list[TraceIndexEntry] = []
        for entry in self.index():
            if task_needle and task_needle not in entry.task_id.lower():
                continue
            if model_needle and model_needle not in entry.model.lower():
                continue
            if text_needle and not (
                text_needle in entry.trace_id.lower()
                or text_needle in entry.task_id.lower()
                or text_needle in entry.sha256.lower()
            ):
                continue
            results.append(entry)
        return results

    # -- Internals ----------------------------------------------------------

    def _lookup_entry(self, trace_id: str) -> TraceIndexEntry | None:
        if not trace_id:
            return None
        for entry in self._read_index():
            if trace_id in (entry.trace_id, entry.sha256):
                return entry
        return None

    def _read_index(self) -> Iterator[TraceIndexEntry]:
        if not self.index_path.exists():
            return iter(())
        return self._iter_index_file()

    def _iter_index_file(self) -> Iterator[TraceIndexEntry]:
        with self.index_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    yield TraceIndexEntry.from_dict(obj)

    def _upsert_index(self, entry: TraceIndexEntry) -> None:
        existing = [e for e in self._read_index() if e.sha256 != entry.sha256]
        existing.append(entry)
        existing.sort(key=lambda e: e.started_at, reverse=True)
        self._write_index(existing)

    def _write_index(self, entries: list[TraceIndexEntry]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = self.index_path.with_suffix(self.index_path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for entry in entries:
                fh.write(json.dumps(entry.to_dict(), sort_keys=True))
                fh.write("\n")
        tmp.replace(self.index_path)


# ---------------------------------------------------------------------------
# Viewer (FastAPI factory)
# ---------------------------------------------------------------------------

_VIEWER_INDEX_HTML: Final = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>Bernstein local trace viewer</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; margin: 0; padding: 1.5rem; color: #1f2328; }
  h1 { font-size: 1.25rem; margin: 0 0 1rem; }
  form { margin-bottom: 1rem; display: flex; gap: 0.5rem; flex-wrap: wrap; }
  input[type=text] { padding: 0.35rem 0.5rem; border: 1px solid #d0d7de; border-radius: 4px; }
  button { padding: 0.35rem 0.75rem; border: 1px solid #d0d7de;
           background: #f6f8fa; border-radius: 4px; cursor: pointer; }
  table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
  th, td { text-align: left; padding: 0.35rem 0.5rem; border-bottom: 1px solid #eaeef2; vertical-align: top; }
  th { background: #f6f8fa; }
  td.sha { font-family: ui-monospace, SFMono-Regular, monospace; color: #57606a; }
  td.id { font-family: ui-monospace, SFMono-Regular, monospace; }
  a { color: #0969da; text-decoration: none; }
  a:hover { text-decoration: underline; }
  .meta { color: #57606a; font-size: 0.85rem; margin-bottom: 0.5rem; }
</style>
</head>
<body>
<h1>Bernstein local trace viewer</h1>
<p class="meta">Root: <code>__ROOT__</code> &middot; __COUNT__ traces indexed</p>
<form method="get" action="/">
  <input type="text" name="task" placeholder="task id" value="__TASK__" />
  <input type="text" name="model" placeholder="model" value="__MODEL__" />
  <input type="text" name="q" placeholder="search" value="__Q__" />
  <button type="submit">Filter</button>
  <a href="/">Reset</a>
</form>
<table>
  <thead>
    <tr>
      <th>started</th><th>task</th><th>trace</th><th>model</th>
      <th>cost</th><th>bytes</th><th>sha256</th><th>actions</th>
    </tr>
  </thead>
  <tbody>
__ROWS__
  </tbody>
</table>
</body>
</html>
"""


def _render_index_html(
    store: ContentAddressedTraceStore,
    entries: list[TraceIndexEntry],
    *,
    task: str,
    model: str,
    q: str,
) -> str:
    import html as _html

    rows: list[str] = [
        (
            "<tr>"
            f"<td>{_html.escape(_fmt_ts(entry.started_at))}</td>"
            f'<td class="id">{_html.escape(entry.task_id) or "-"}</td>'
            f'<td class="id">{_html.escape(entry.trace_id)}</td>'
            f"<td>{_html.escape(entry.model) or '-'}</td>"
            f"<td>{entry.cost_usd:.4f}</td>"
            f"<td>{entry.byte_size}</td>"
            f'<td class="sha">{_html.escape(entry.sha256[:12])}</td>'
            "<td>"
            f'<a href="/traces/{_html.escape(entry.trace_id)}">json</a> '
            f'<a href="/traces/{_html.escape(entry.trace_id)}/timeline">timeline</a>'
            "</td>"
            "</tr>"
        )
        for entry in entries
    ]
    if not rows:
        rows.append(
            '<tr><td colspan="8" style="text-align:center; color:#57606a; padding:1rem;">'
            "No traces match the current filter.</td></tr>"
        )
    total_indexed = len(store.index())
    return (
        _VIEWER_INDEX_HTML.replace("__ROOT__", _html.escape(str(store.root)))
        .replace("__COUNT__", str(total_indexed))
        .replace("__TASK__", _html.escape(task))
        .replace("__MODEL__", _html.escape(model))
        .replace("__Q__", _html.escape(q))
        .replace("__ROWS__", "\n".join(rows))
    )


def _fmt_ts(ts: float) -> str:
    import datetime as _dt

    if not ts:
        return "-"
    try:
        return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError, OverflowError):
        return "-"


def build_viewer_app(store: ContentAddressedTraceStore) -> Any:
    """Return a FastAPI application that serves the local viewer.

    The app exposes:

    * ``GET /`` - HTML index with task / model / free-text filters.
    * ``GET /traces/{trace_id}`` - pretty-printed JSON or JSONL body.
    * ``GET /traces/{trace_id}/timeline`` - timeline of trace steps.
    * ``GET /api/traces`` - JSON list mirror of the index (HTMX-friendly).
    * ``GET /api/traces/{trace_id}`` - JSON body of a single trace.

    Importing ``fastapi`` lazily keeps the rest of the module importable
    in environments where the optional viewer extra is not installed.
    """
    from fastapi import FastAPI, HTTPException, Query
    from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

    app = FastAPI(title="Bernstein trace viewer", docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    def _index(
        # NOSONAR python:S8410 - Annotated[..., Query()] cannot be used here:
        # ``Query`` is imported lazily inside this function (optional viewer
        # extra), so FastAPI's get_type_hints cannot resolve the stringized
        # annotation under ``from __future__ import annotations``.
        task: str = Query(default=""),  # NOSONAR python:S8410
        model: str = Query(default=""),  # NOSONAR python:S8410
        q: str = Query(default=""),  # NOSONAR python:S8410
    ) -> HTMLResponse:
        entries = store.search(task_id=task, model=model, text=q)
        return HTMLResponse(_render_index_html(store, entries, task=task, model=model, q=q))

    @app.get("/api/traces")
    def _api_index(
        # NOSONAR python:S8410 - see _index above; lazy local ``Query`` import
        # prevents the Annotated form from resolving at route registration.
        task: str = Query(default=""),  # NOSONAR python:S8410
        model: str = Query(default=""),  # NOSONAR python:S8410
        q: str = Query(default=""),  # NOSONAR python:S8410
    ) -> JSONResponse:
        entries = store.search(task_id=task, model=model, text=q)
        return JSONResponse({"traces": [e.to_dict() for e in entries]})

    @app.get("/api/traces/{trace_id}")
    def _api_trace(trace_id: str) -> JSONResponse:
        raw = store.get(trace_id)
        if raw is None:
            raise HTTPException(status_code=404, detail="trace not found")
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {"raw": raw.decode("utf-8", errors="replace")}
        return JSONResponse({"trace_id": trace_id, "body": body})

    @app.get("/traces/{trace_id}", response_class=PlainTextResponse)
    def _show_trace(trace_id: str) -> PlainTextResponse:
        raw = store.get(trace_id)
        if raw is None:
            raise HTTPException(status_code=404, detail="trace not found")
        try:
            pretty = json.dumps(json.loads(raw), indent=2, sort_keys=True)
        except json.JSONDecodeError:
            pretty = raw.decode("utf-8", errors="replace")
        return PlainTextResponse(pretty, media_type="application/json")

    @app.get("/traces/{trace_id}/timeline", response_class=HTMLResponse)
    def _timeline(trace_id: str) -> HTMLResponse:
        raw = store.get(trace_id)
        if raw is None:
            raise HTTPException(status_code=404, detail="trace not found")
        return HTMLResponse(_render_timeline_html(trace_id, raw))

    return app


_TIMELINE_HTML: Final = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>Trace __TRACE__</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; margin: 0; padding: 1.5rem; color: #1f2328; }
  h1 { font-size: 1.1rem; margin: 0 0 1rem; }
  ol { padding-left: 1.5rem; }
  li { margin-bottom: 0.4rem; font-size: 0.9rem; }
  .type { display: inline-block; padding: 0 0.4rem; border-radius: 3px;
          background: #ddf4ff; color: #0969da; margin-right: 0.4rem;
          font-family: ui-monospace, monospace; font-size: 0.8rem; }
  .ts { color: #57606a; font-size: 0.8rem; margin-left: 0.4rem; }
  .detail { color: #1f2328; }
  a { color: #0969da; }
</style>
</head>
<body>
<h1>Trace timeline: <code>__TRACE__</code> &middot;
    <a href="/">back</a> &middot;
    <a href="/traces/__TRACE__">json</a></h1>
__BODY__
</body>
</html>
"""


def _render_timeline_html(trace_id: str, raw: bytes) -> str:
    import html as _html

    steps: list[dict[str, Any]] = []
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            steps_field = obj.get("steps")
            if isinstance(steps_field, list):
                steps = [s for s in steps_field if isinstance(s, dict)]
    except json.JSONDecodeError:
        for line in raw.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                steps.append(obj)

    if not steps:
        body = "<p>No timeline steps recorded.</p>"
    else:
        items: list[str] = []
        for step in steps:
            step_type = _html.escape(str(step.get("type", "step")))
            detail = _html.escape(str(step.get("detail", "")))
            ts = _fmt_ts(_coerce_float(step.get("timestamp")) or 0.0)
            items.append(
                f'<li><span class="type">{step_type}</span>'
                f'<span class="detail">{detail}</span>'
                f'<span class="ts">{_html.escape(ts)}</span></li>'
            )
        body = "<ol>" + "\n".join(items) + "</ol>"

    return _TIMELINE_HTML.replace("__TRACE__", _html.escape(trace_id)).replace("__BODY__", body)


__all__ = [
    "ContentAddressedTraceStore",
    "TraceIndexEntry",
    "TraceMetadataHints",
    "build_viewer_app",
]
