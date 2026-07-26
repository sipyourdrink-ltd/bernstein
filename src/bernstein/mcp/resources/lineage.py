"""MCP resources + tool exposing lineage to clients.

Per ADR-009 §7:

  * ``lineage://artefact/<repo-relative-path>``
      → JSONL stream of every entry that touched that artefact.
  * ``lineage://stats``
      → JSON summary (entry count, unique artefact count, open-fork count,
      agents seen, last 24h activity).
  * Tool ``verify_chain(artefact_path)``
      → ``{"ok": bool, "reason": str | None}``. Walks the chain for one
      artefact, checks each entry's parent_hashes is reachable in the log
      and that the canonical bytes still hash to the recorded ``entry_hash``.

Default-off in remote/SSE MCP, on for local stdio. Callers pass
``enabled=False`` for the remote registrar - the function then returns
``False`` without touching the FastMCP instance.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mcp.server.fastmcp.resources.templates import ResourceTemplate

from bernstein.core.lineage.entry import canonicalise, entry_hash
from bernstein.core.lineage.store import LineageStore
from bernstein.mcp.input_validation import validate_or_error, validation_error_response

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)


class _SlashTolerantResourceTemplate(ResourceTemplate):
    """ResourceTemplate variant where ``{name}`` placeholders match path segments containing ``/``.

    The stock template uses ``[^/]+`` which rejects ``src/foo.py``. We override
    ``matches`` to use ``.+`` for the *last* placeholder so it greedily captures
    the repo-relative path the lineage URI carries.
    """

    def matches(self, uri: str) -> dict[str, Any] | None:  # type: ignore[override]
        # Build a regex where every placeholder uses ``.+`` (no slash exclusion).
        # ``.+?`` is non-greedy so multi-placeholder templates still split sensibly;
        # we only have single-placeholder URIs in this module today.
        pattern = re.sub(r"\{([^}]+)\}", r"(?P<\1>.+)", self.uri_template)
        match = re.match(f"^{pattern}$", uri)
        if match:
            return match.groupdict()
        return None


def lineage_artefact_body(root: Path, artefact_path: str) -> str:
    """JSONL chain of lineage entries for ``artefact_path``.

    Shared by the FastMCP resource below and the streamable HTTP transport's
    ``resources/read`` (#3084), so both surfaces serve the same bytes.
    """
    store = LineageStore(root)
    lines: list[str] = []
    for entry, _jws in store.read_log():
        if entry.artefact_path != artefact_path:
            continue
        lines.append(canonicalise(entry).decode("utf-8"))
    return "\n".join(lines)


def lineage_stats_body(root: Path) -> str:
    """JSON summary counts over the entire lineage log (shared, see above)."""
    store = LineageStore(root)
    total = 0
    artefacts: set[str] = set()
    agents: set[str] = set()
    last_24h = 0
    now_ns = time.time_ns()
    cutoff = now_ns - 24 * 3_600 * 1_000_000_000
    # Track open forks by counting artefacts whose tip_set has >1 open.
    for entry, _jws in store.read_log():
        total += 1
        artefacts.add(entry.artefact_path)
        agents.add(entry.agent_id)
        if entry.ts_ns >= cutoff:
            last_24h += 1
    open_forks = sum(1 for path in artefacts if len(store.tip_set(path).get("open", [])) > 1)
    payload = {
        "total_entries": total,
        "artefacts": len(artefacts),
        "open_forks": open_forks,
        "agents_seen": sorted(agents),
        "last_24h_entries": last_24h,
    }
    return json.dumps(payload, sort_keys=True)


def register_lineage_resources(
    mcp: FastMCP[None],
    *,
    lineage_root: Path,
    enabled: bool = True,
) -> bool:
    """Register the lineage resources + verify_chain tool on ``mcp``.

    Args:
        mcp: FastMCP instance to mount on.
        lineage_root: Root of the lineage store (typically
            ``<repo>/.sdd/lineage``).
        enabled: When ``False``, return immediately without registering.
            Callers should pass ``enabled=False`` in remote transports
            unless they've explicitly opted in.

    Returns:
        ``True`` when the resources were registered, ``False`` when disabled.
    """
    if not enabled:
        logger.info("MCP lineage resources disabled (enabled=False)")
        return False

    root = Path(lineage_root)

    def lineage_artefact(artefact_path: str) -> str:
        """JSONL chain of lineage entries for ``artefact_path``."""
        return lineage_artefact_body(root, artefact_path)

    # Bypass the FastMCP decorator for this template because the default
    # placeholder regex (``[^/]+``) rejects path segments containing ``/``.
    # The lineage URI is ``lineage://artefact/<repo-relative-path>`` and the
    # path almost always contains a slash, so we register a slash-tolerant
    # subclass directly on the resource manager.
    artefact_template = _SlashTolerantResourceTemplate.from_function(
        lineage_artefact,
        uri_template="lineage://artefact/{artefact_path}",
        name="lineage_artefact",
        description="JSONL chain of lineage entries for a single artefact.",
        mime_type="application/x-ndjson",
    )
    mcp._resource_manager._templates[artefact_template.uri_template] = artefact_template

    @mcp.resource(
        "lineage://stats",
        name="lineage_stats",
        description="Summary counts over the entire lineage log.",
        mime_type="application/json",
    )
    def lineage_stats() -> str:  # pyright: ignore[reportUnusedFunction]
        return lineage_stats_body(root)

    @mcp.tool(
        name="bernstein_verify_lineage",
        description="Verify the lineage chain of a single artefact path.",
    )
    def bernstein_verify_lineage(artefact_path: str) -> str:  # pyright: ignore[reportUnusedFunction]
        err = validate_or_error("bernstein_verify_lineage", {"artefact_path": artefact_path})
        if err is not None:
            return validation_error_response(err)
        store = LineageStore(root)
        ok, reason = _verify_chain(store, artefact_path)
        return json.dumps({"ok": ok, "reason": reason})

    from bernstein.core.protocols.mcp.tool_tiers import deprecated_aliases_enabled

    if deprecated_aliases_enabled():
        # Deprecated alias (#3087): callable, never advertised, removed in
        # ALIAS_REMOVAL_RELEASE. Registered here rather than with the other
        # aliases because it needs the lineage store root.
        @mcp.tool(
            name="verify_chain",
            description="Deprecated alias of bernstein_verify_lineage.",
        )
        def verify_chain(artefact_path: str) -> str:  # pyright: ignore[reportUnusedFunction]
            from bernstein.mcp.server import _deprecated_alias_payload  # pyright: ignore[reportPrivateUsage]

            err = validate_or_error("verify_chain", {"artefact_path": artefact_path})
            if err is not None:
                return validation_error_response(err)
            store = LineageStore(root)
            ok, reason = _verify_chain(store, artefact_path)
            return _deprecated_alias_payload("verify_chain", json.dumps({"ok": ok, "reason": reason}))

    return True


def _verify_chain(store: LineageStore, artefact_path: str) -> tuple[bool, str | None]:
    """Walk the chain for ``artefact_path``; return ``(ok, reason_or_none)``.

    Checks:

      1. The canonical bytes of each entry as it appears in ``log.jsonl``
         match the bytes we re-canonicalise from the parsed entry. Catches
         in-place byte flips that ``json.loads`` would accept.
      2. Every parent referenced by an entry exists in the log.
    """
    entries: list[tuple[str, list[str], str]] = []  # (entry_hash, parent_hashes, artefact_path)
    seen_hashes: set[str] = set()

    for entry, _jws in store.read_log():
        h = entry_hash(entry)
        seen_hashes.add(h)
        if entry.artefact_path == artefact_path:
            entries.append((h, list(entry.parent_hashes), entry.artefact_path))

    if not entries:
        return True, None  # nothing recorded for this artefact - trivially valid

    # The canonical-bytes byte-equality check needs raw bytes from the log,
    # not the parsed entry - re-read the raw lines and verify each one
    # round-trips through canonicalise().
    log_path = store.log_path
    if log_path.exists():
        for raw in log_path.read_bytes().rstrip(b"\n").split(b"\n"):
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                return False, f"invalid JSON in log.jsonl: {exc}"
            # Round-trip through the canonical form via the dict. If raw
            # bytes don't match the canonical re-serialisation, something
            # was injected (e.g. extra whitespace, reordered keys, or a
            # tampered field value that still parses).
            recanon = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            if recanon != raw:
                return False, "non-canonical line bytes in log.jsonl"

    for h, parents, _path in entries:
        for p in parents:
            if p not in seen_hashes:
                return False, f"dangling parent {p[:24]}… for entry {h[:24]}…"

    return True, None


__all__ = ["lineage_artefact_body", "lineage_stats_body", "register_lineage_resources"]
