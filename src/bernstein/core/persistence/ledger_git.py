"""Git-ref anchoring for the durable work ledger (#2358).

The work ledger is portable state: it must travel with the repository it
describes so a clone on any machine can resume the run. This module stores
the validated chain under a dedicated side ref::

    refs/bernstein/work-ledger/<run-id>

following the same namespace discipline as the per-tool-call snapshot refs
(:mod:`bernstein.core.git.snapshot`): the ref never pollutes the branch
list and is not pushed by a default ``git push``.

Layout inside the ref
---------------------
Each anchor is one commit whose tree contains:

* ``LEDGER.json`` -- canonical metadata ``{chunk_count, entry_count,
  format_version, head_hash, run_id}``. No timestamps, so the tree is a
  *deterministic projection* of the chain: two operators anchoring the
  same chain produce byte-identical trees (the tree sha is the anchor's
  verifiable identity; commit metadata carries wall-clock context).
* ``chunk-<n>.jsonl`` -- the validated canonical ledger lines, chunked so
  a very long run never creates one giant blob. Concatenating the chunks
  in name order reproduces the on-disk bucket byte-for-byte.

Re-anchoring an extended chain creates a child commit of the previous
anchor, so the ref history records every anchor point. The gc policy
(:func:`gc_ledger_ref`) squashes that history to a single parentless
commit -- the chunks of dropped anchors become unreachable and a normal
``git gc`` reclaims them, which is the repo-bloat mitigation for
long-running goals.

Fail-closed and divergence contract
-----------------------------------
* A chain that does not verify is never anchored.
* Anchoring compares the local chain against the currently anchored one:
  a diverged pair (two chains extending the same parent entry -- i.e. two
  divergent resumes) raises :class:`LedgerDivergenceError` naming the
  exact fork entry and both heads. Divergence is an explicit, detected
  error, never a silent merge.
* Materializing an anchored chain onto a machine with an existing local
  ledger applies the same comparison and only ever fast-forwards.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.git.git_basic import run_git
from bernstein.core.persistence.work_ledger import (
    ChainRelation,
    LedgerEntry,
    LedgerError,
    LedgerReader,
    compare_chains,
    validated_canonical_lines,
    verify_entry_rows,
)

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

#: Namespace for work-ledger refs; never pushed by a default ``git push``.
LEDGER_REF_PREFIX = "refs/bernstein/work-ledger/"

#: Metadata filename inside every anchor tree.
LEDGER_META_NAME = "LEDGER.json"

#: Anchor tree format version; bump on layout changes.
LEDGER_FORMAT_VERSION = 1

#: Default number of canonical lines per chunk blob.
DEFAULT_CHUNK_LINES = 1000

#: Commit identity for anchor commits. The tree sha is the verifiable
#: identity of an anchor; the commit exists to give the ref a history, so
#: it uses a fixed machine identity rather than the operator's.
_ANCHOR_IDENT_NAME = "bernstein-work-ledger"
_ANCHOR_IDENT_EMAIL = "work-ledger@bernstein.invalid"

#: Run ids share the git-ref-safe alphabet used by snapshot/stack refs.
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")


class LedgerGitError(RuntimeError):
    """Raised when a ledger anchor/fetch/materialize step cannot proceed."""


class LedgerDivergenceError(LedgerGitError):
    """Two chains extend the same parent entry (divergent resumes).

    Attributes:
        fork_seq: Seq of the first differing entry.
        local_head: Head hash of the local chain.
        remote_head: Head hash of the anchored/remote chain.
    """

    def __init__(self, *, fork_seq: int, local_head: str, remote_head: str, context: str) -> None:
        self.fork_seq = fork_seq
        self.local_head = local_head
        self.remote_head = remote_head
        message = (
            f"work ledgers diverge at entry {fork_seq}: two chains extend the same "
            f"parent entry (local head {local_head[:16]}..., anchored head "
            f"{remote_head[:16]}...). Two divergent resumes of this run exist; "
            f"refusing to {context}. Inspect both chains with 'bernstein ledger "
            f"verify --json', keep exactly one lineage, and re-anchor it."
        )
        super().__init__(message)


@dataclass(frozen=True)
class LedgerAnchor:
    """Result of anchoring a chain to the ledger ref."""

    run_id: str
    ref: str
    commit_sha: str
    tree_sha: str
    head_hash: str
    entry_count: int
    chunk_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "ref": self.ref,
            "commit_sha": self.commit_sha,
            "tree_sha": self.tree_sha,
            "head_hash": self.head_hash,
            "entry_count": self.entry_count,
            "chunk_count": self.chunk_count,
        }


@dataclass(frozen=True)
class MaterializeResult:
    """Result of materializing an anchored chain into a ledger directory."""

    action: str  # "created" | "fast-forwarded" | "unchanged"
    head_hash: str
    entry_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "head_hash": self.head_hash,
            "entry_count": self.entry_count,
        }


@dataclass(frozen=True)
class GcResult:
    """Result of squashing a ledger ref's anchor history."""

    dropped_commits: int
    commit_sha: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "dropped_commits": self.dropped_commits,
            "commit_sha": self.commit_sha,
        }


# ---------------------------------------------------------------------------
# Ref naming
# ---------------------------------------------------------------------------


def ledger_ref(run_id: str) -> str:
    """Return the full ref path for *run_id*, validating the id.

    Raises:
        LedgerGitError: When the run id is not git-ref safe.
    """
    if not _RUN_ID_RE.match(run_id):
        msg = f"invalid run id {run_id!r}: must match {_RUN_ID_RE.pattern}"
        raise LedgerGitError(msg)
    return f"{LEDGER_REF_PREFIX}{run_id}"


def _resolve_ref(repo_dir: Path, ref: str) -> str | None:
    """Return the sha *ref* points at, or ``None`` when absent."""
    result = run_git(["rev-parse", "--verify", "--quiet", ref], repo_dir)
    sha = result.stdout.strip()
    if result.returncode != 0 or not sha:
        return None
    return sha


def list_ledger_runs(repo_dir: Path) -> list[str]:
    """Return the run ids that have an anchored ledger ref in *repo_dir*."""
    result = run_git(
        ["for-each-ref", "--format=%(refname)", LEDGER_REF_PREFIX],
        repo_dir,
    )
    if result.returncode != 0:
        msg = f"git for-each-ref failed: {result.stderr.strip()}"
        raise LedgerGitError(msg)
    runs: list[str] = []
    for line in result.stdout.splitlines():
        name = line.strip()
        if name.startswith(LEDGER_REF_PREFIX):
            runs.append(name[len(LEDGER_REF_PREFIX) :])
    return sorted(runs)


# ---------------------------------------------------------------------------
# Reading an anchored chain
# ---------------------------------------------------------------------------


def _read_blob(repo_dir: Path, sha: str) -> bytes:
    result = subprocess.run(
        ["git", "cat-file", "blob", sha],
        cwd=repo_dir,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        msg = f"git cat-file blob {sha[:12]} failed: {stderr}"
        raise LedgerGitError(msg)
    return result.stdout


def _read_tree_entries(repo_dir: Path, ref: str) -> dict[str, str]:
    """Return ``{name: blob_sha}`` for the flat tree at *ref*."""
    result = run_git(["ls-tree", "-z", ref], repo_dir)
    if result.returncode != 0:
        msg = f"git ls-tree {ref} failed: {result.stderr.strip()}"
        raise LedgerGitError(msg)
    entries: dict[str, str] = {}
    for record in result.stdout.split("\0"):
        if not record.strip():
            continue
        meta, _, name = record.partition("\t")
        parts = meta.split()
        if len(parts) != 3 or parts[1] != "blob":
            continue
        entries[name] = parts[2]
    return entries


def read_anchored_rows(repo_dir: Path, run_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read the anchored chain for *run_id*; return ``(rows, meta)``.

    Raises:
        LedgerGitError: When the ref is missing, the tree is malformed, or
            a chunk contains an unparseable row.
    """
    ref = ledger_ref(run_id)
    if _resolve_ref(repo_dir, ref) is None:
        msg = f"no anchored ledger for run {run_id!r} (ref {ref} not found)"
        raise LedgerGitError(msg)

    entries = _read_tree_entries(repo_dir, ref)
    meta_sha = entries.get(LEDGER_META_NAME)
    if meta_sha is None:
        msg = f"anchored ledger for run {run_id!r} is missing {LEDGER_META_NAME}"
        raise LedgerGitError(msg)
    try:
        meta_raw: Any = json.loads(_read_blob(repo_dir, meta_sha).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        msg = f"anchored ledger metadata for run {run_id!r} is not valid JSON"
        raise LedgerGitError(msg) from exc
    meta: dict[str, Any] = meta_raw if isinstance(meta_raw, dict) else {}

    rows: list[dict[str, Any]] = []
    chunk_names = sorted(name for name in entries if name != LEDGER_META_NAME)
    for name in chunk_names:
        payload = _read_blob(repo_dir, entries[name]).decode("utf-8", errors="replace")
        for offset, raw in enumerate(payload.splitlines(), start=1):
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                row: Any = json.loads(stripped)
            except json.JSONDecodeError as exc:
                msg = f"anchored ledger chunk {name} line {offset} is not valid JSON"
                raise LedgerGitError(msg) from exc
            if not isinstance(row, dict):
                msg = f"anchored ledger chunk {name} line {offset} is not a JSON object"
                raise LedgerGitError(msg)
            rows.append(row)
    return rows, meta


def _anchored_entries(rows: list[dict[str, Any]]) -> list[LedgerEntry]:
    return [LedgerEntry.from_dict(row) for row in rows]


def _verify_anchored(rows: list[dict[str, Any]], meta: dict[str, Any], run_id: str) -> str:
    """Verify the anchored chain end to end; return its head hash."""
    verification = verify_entry_rows(rows)
    if not verification.ok:
        head = "; ".join(verification.errors[:3])
        msg = f"anchored ledger for run {run_id!r} fails verification: {head}"
        raise LedgerGitError(msg)
    meta_head = str(meta.get("head_hash", ""))
    if meta_head and meta_head != verification.head_hash:
        msg = (
            f"anchored ledger for run {run_id!r} metadata head {meta_head[:16]}... "
            f"does not match the walked chain head {verification.head_hash[:16]}..."
        )
        raise LedgerGitError(msg)
    return verification.head_hash


# ---------------------------------------------------------------------------
# Anchoring (export)
# ---------------------------------------------------------------------------


def _hash_blob(repo_dir: Path, payload: bytes) -> str:
    result = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=repo_dir,
        input=payload,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        msg = f"git hash-object failed: {stderr}"
        raise LedgerGitError(msg)
    return result.stdout.decode("utf-8").strip()


def _mktree(repo_dir: Path, entries: list[tuple[str, str]]) -> str:
    """Build a flat tree from ``(name, blob_sha)`` pairs; return the sha."""
    lines = "".join(f"100644 blob {sha}\t{name}\0" for name, sha in sorted(entries))
    result = subprocess.run(
        ["git", "mktree", "-z"],
        cwd=repo_dir,
        input=lines.encode("utf-8"),
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        msg = f"git mktree failed: {stderr}"
        raise LedgerGitError(msg)
    return result.stdout.decode("utf-8").strip()


def _commit_tree(repo_dir: Path, tree_sha: str, *, parent: str | None, message: str) -> str:
    """Create an anchor commit for *tree_sha* with a fixed machine identity."""
    args = ["git", "commit-tree", tree_sha, "-m", message]
    if parent is not None:
        args[3:3] = ["-p", parent]
    env = os.environ | {
        "GIT_AUTHOR_NAME": _ANCHOR_IDENT_NAME,
        "GIT_AUTHOR_EMAIL": _ANCHOR_IDENT_EMAIL,
        "GIT_COMMITTER_NAME": _ANCHOR_IDENT_NAME,
        "GIT_COMMITTER_EMAIL": _ANCHOR_IDENT_EMAIL,
    }
    result = subprocess.run(
        args,
        cwd=repo_dir,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        msg = f"git commit-tree failed: {result.stderr.strip()}"
        raise LedgerGitError(msg)
    return result.stdout.strip()


def _canonical_meta(run_id: str, *, head_hash: str, entry_count: int, chunk_count: int) -> bytes:
    document = {
        "chunk_count": chunk_count,
        "entry_count": entry_count,
        "format_version": LEDGER_FORMAT_VERSION,
        "head_hash": head_hash,
        "run_id": run_id,
    }
    return (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _compare_with_anchored(
    repo_dir: Path,
    run_id: str,
    local_entries: list[LedgerEntry],
) -> ChainRelation | None:
    """Compare the local chain with the anchored one, or ``None`` if no ref."""
    ref = ledger_ref(run_id)
    if _resolve_ref(repo_dir, ref) is None:
        return None
    rows, meta = read_anchored_rows(repo_dir, run_id)
    _verify_anchored(rows, meta, run_id)
    return compare_chains(local_entries, _anchored_entries(rows))


def anchor_ledger(
    repo_dir: Path,
    ledger_dir: Path,
    *,
    run_id: str,
    chunk_lines: int = DEFAULT_CHUNK_LINES,
) -> LedgerAnchor:
    """Anchor the validated chain at *ledger_dir* to the ledger ref.

    Fail-closed: the chain is verified before anything reaches the ref, a
    torn trailing line from a crash is excluded exactly like writer
    recovery, and a diverged anchored chain refuses the anchor rather than
    overwriting it.

    Args:
        repo_dir: Repository whose ref namespace receives the anchor.
        ledger_dir: Per-run ledger directory holding the bucket file.
        run_id: Run id; becomes the ref suffix.
        chunk_lines: Canonical lines per chunk blob.

    Returns:
        A :class:`LedgerAnchor` describing the ref state.

    Raises:
        LedgerGitError: On a missing/broken chain or a git failure.
        LedgerDivergenceError: When the anchored chain diverges from the
            local one (two divergent resumes).
    """
    if chunk_lines < 1:
        msg = f"chunk_lines must be >= 1, got {chunk_lines}"
        raise LedgerGitError(msg)
    ref = ledger_ref(run_id)
    try:
        lines, head_hash = validated_canonical_lines(ledger_dir)
    except LedgerError as exc:
        msg = f"refusing to anchor run {run_id!r}: {exc}"
        raise LedgerGitError(msg) from exc
    if not lines:
        msg = f"no work ledger entries to anchor at {ledger_dir}"
        raise LedgerGitError(msg)

    local_entries = list(LedgerReader(ledger_dir).entries())
    relation = _compare_with_anchored(repo_dir, run_id, local_entries)
    parent = _resolve_ref(repo_dir, ref)
    if relation is not None:
        if relation.relation == "diverged":
            raise LedgerDivergenceError(
                fork_seq=relation.fork_seq or 0,
                local_head=relation.local_head,
                remote_head=relation.remote_head,
                context="anchor",
            )
        if relation.relation == "remote-ahead":
            msg = (
                f"anchored ledger for run {run_id!r} is ahead of the local chain "
                f"({relation.remote_entries} > {relation.local_entries} entries); "
                f"run 'bernstein ledger fetch {run_id}' first."
            )
            raise LedgerGitError(msg)
        if relation.relation == "identical" and parent is not None:
            tree_sha = run_git(["rev-parse", f"{ref}^{{tree}}"], repo_dir).stdout.strip()
            return LedgerAnchor(
                run_id=run_id,
                ref=ref,
                commit_sha=parent,
                tree_sha=tree_sha,
                head_hash=head_hash,
                entry_count=len(lines),
                chunk_count=len([name for name in _read_tree_entries(repo_dir, ref) if name != LEDGER_META_NAME]),
            )

    chunks = [lines[i : i + chunk_lines] for i in range(0, len(lines), chunk_lines)]
    tree_entries: list[tuple[str, str]] = []
    for index, chunk in enumerate(chunks):
        payload = ("\n".join(chunk) + "\n").encode("utf-8")
        tree_entries.append((f"chunk-{index:06d}.jsonl", _hash_blob(repo_dir, payload)))
    meta_payload = _canonical_meta(
        run_id,
        head_hash=head_hash,
        entry_count=len(lines),
        chunk_count=len(chunks),
    )
    tree_entries.append((LEDGER_META_NAME, _hash_blob(repo_dir, meta_payload)))

    tree_sha = _mktree(repo_dir, tree_entries)
    message = f"bernstein work ledger anchor\n\nrun_id={run_id}\nhead={head_hash}\nentries={len(lines)}"
    commit_sha = _commit_tree(repo_dir, tree_sha, parent=parent, message=message)
    result = run_git(["update-ref", ref, commit_sha], repo_dir)
    if result.returncode != 0:
        msg = f"git update-ref {ref} failed: {result.stderr.strip()}"
        raise LedgerGitError(msg)

    logger.info(
        "anchored work ledger run=%s entries=%d chunks=%d ref=%s",
        run_id,
        len(lines),
        len(chunks),
        ref,
    )
    return LedgerAnchor(
        run_id=run_id,
        ref=ref,
        commit_sha=commit_sha,
        tree_sha=tree_sha,
        head_hash=head_hash,
        entry_count=len(lines),
        chunk_count=len(chunks),
    )


# ---------------------------------------------------------------------------
# Fetch + materialize (import)
# ---------------------------------------------------------------------------


def fetch_ledger_ref(repo_dir: Path, run_id: str, *, remote: str = "origin") -> str:
    """Fetch the anchored ledger ref for *run_id* from *remote*.

    The fetch is forced: the ref is derived data (an anchor of a chain);
    the divergence decision is made against the local *ledger file* at
    materialize time, where the fork position can be named exactly.

    Returns:
        The sha the local ref points at after the fetch.

    Raises:
        LedgerGitError: When the remote has no such ref or the fetch fails.
    """
    ref = ledger_ref(run_id)
    result = run_git(["fetch", "--no-tags", remote, f"+{ref}:{ref}"], repo_dir, timeout=120)
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "couldn't find remote ref" in stderr.lower():
            msg = f"no anchored ledger for run {run_id!r} on remote {remote!r}"
            raise LedgerGitError(msg)
        msg = f"git fetch of {ref} from {remote!r} failed: {stderr}"
        raise LedgerGitError(msg)
    sha = _resolve_ref(repo_dir, ref)
    if sha is None:
        msg = f"ledger ref {ref} missing after fetch from {remote!r}"
        raise LedgerGitError(msg)
    return sha


def materialize_ledger(repo_dir: Path, run_id: str, ledger_dir: Path) -> MaterializeResult:
    """Materialize the anchored chain for *run_id* into *ledger_dir*.

    The anchored chain is verified end to end before a byte is written.
    An existing local ledger is compared against it: identical chains are
    a no-op, an anchored extension fast-forwards the local file, a local
    extension refuses (anchor instead), and a diverged pair refuses with
    the exact fork position (two divergent resumes are never merged).

    Raises:
        LedgerGitError: Missing ref, broken anchored chain, or local-ahead.
        LedgerDivergenceError: When local and anchored chains diverge.
    """
    rows, meta = read_anchored_rows(repo_dir, run_id)
    head_hash = _verify_anchored(rows, meta, run_id)
    anchored = _anchored_entries(rows)

    reader = LedgerReader(ledger_dir)
    action = "created"
    if reader.exists():
        relation = compare_chains(list(reader.entries()), anchored)
        if relation.relation == "diverged":
            raise LedgerDivergenceError(
                fork_seq=relation.fork_seq or 0,
                local_head=relation.local_head,
                remote_head=relation.remote_head,
                context="materialize",
            )
        if relation.relation == "local-ahead":
            msg = (
                f"local ledger at {ledger_dir} is ahead of the anchored chain "
                f"({relation.local_entries} > {relation.remote_entries} entries); "
                f"run 'bernstein ledger anchor {run_id}' instead."
            )
            raise LedgerGitError(msg)
        if relation.relation == "identical":
            return MaterializeResult(action="unchanged", head_hash=head_hash, entry_count=len(anchored))
        action = "fast-forwarded"

    ledger_dir.mkdir(parents=True, exist_ok=True)
    payload = "\n".join(entry.canonical_line() for entry in anchored) + "\n"
    tmp_path = ledger_dir / ".bucket.materialize.tmp"
    tmp_path.write_text(payload, encoding="utf-8", newline="")
    os.replace(tmp_path, reader.bucket_path)
    if os.name == "posix":
        try:
            ledger_dir.chmod(0o700)
            reader.bucket_path.chmod(0o600)
        except OSError:  # pragma: no cover -- permissions are best-effort
            logger.debug("ledger permission tightening failed for %s", ledger_dir, exc_info=True)

    verification = LedgerReader(ledger_dir).verify(expected_head=head_hash)
    if not verification.ok:  # pragma: no cover -- defensive round-trip check
        msg = f"materialized ledger failed re-verification: {'; '.join(verification.errors[:3])}"
        raise LedgerGitError(msg)

    return MaterializeResult(action=action, head_hash=head_hash, entry_count=len(anchored))


# ---------------------------------------------------------------------------
# GC policy
# ---------------------------------------------------------------------------


def gc_ledger_ref(repo_dir: Path, run_id: str) -> GcResult:
    """Squash the anchor history of *run_id*'s ref to one parentless commit.

    The squashed commit preserves the exact anchored tree (the verifiable
    identity), while the dropped anchor commits and their superseded chunk
    blobs become unreachable so a normal ``git gc`` reclaims the space.

    Raises:
        LedgerGitError: When the ref is missing or a git call fails.
    """
    ref = ledger_ref(run_id)
    head = _resolve_ref(repo_dir, ref)
    if head is None:
        msg = f"no anchored ledger for run {run_id!r} (ref {ref} not found)"
        raise LedgerGitError(msg)

    history = run_git(["rev-list", "--count", ref], repo_dir)
    if history.returncode != 0:
        msg = f"git rev-list {ref} failed: {history.stderr.strip()}"
        raise LedgerGitError(msg)
    count = int(history.stdout.strip() or "1")
    if count <= 1:
        return GcResult(dropped_commits=0, commit_sha=head)

    tree_sha = run_git(["rev-parse", f"{ref}^{{tree}}"], repo_dir).stdout.strip()
    message = f"bernstein work ledger anchor (gc squash)\n\nrun_id={run_id}"
    commit_sha = _commit_tree(repo_dir, tree_sha, parent=None, message=message)
    result = run_git(["update-ref", ref, commit_sha, head], repo_dir)
    if result.returncode != 0:
        msg = f"git update-ref {ref} failed during gc: {result.stderr.strip()}"
        raise LedgerGitError(msg)
    return GcResult(dropped_commits=count - 1, commit_sha=commit_sha)


__all__ = [
    "DEFAULT_CHUNK_LINES",
    "LEDGER_FORMAT_VERSION",
    "LEDGER_META_NAME",
    "LEDGER_REF_PREFIX",
    "GcResult",
    "LedgerAnchor",
    "LedgerDivergenceError",
    "LedgerGitError",
    "MaterializeResult",
    "anchor_ledger",
    "fetch_ledger_ref",
    "gc_ledger_ref",
    "ledger_ref",
    "list_ledger_runs",
    "materialize_ledger",
    "read_anchored_rows",
]
