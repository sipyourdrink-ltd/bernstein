"""Clean-run attestation: prove task ground-truth was never fetched (#2930).

An eval score is only meaningful when the agent solved the task without ever
seeing the answer. This module turns that assumption into a signed, replayable
artefact following the receipt-is-the-proof pattern of
:mod:`bernstein.eval.gate_receipt`:

* the task's ground-truth (id, title, completion signals, expected test
  commands, reference-solution contents) is sealed into a
  :class:`ContrabandSet` of keyed HMAC digests under the operator's audit key,
  so the attestation commits to the answer without ever carrying it. The
  task's own golden-source material is derived automatically
  (:func:`derive_task_reference_blobs`) and coverage counts are sealed into
  the commitment, so reference content can neither be silently omitted nor
  silently absent -- declared-but-unloadable reference content refuses to
  seal (:class:`CleanRunCommitmentError`);
* the closed universe of in-scope reads comes from the substrate, not from
  config: the task's worktree root plus the allowed endpoints of a
  :class:`~bernstein.core.security.network_isolation.NetworkPolicy`. Without a
  bounded worktree root the builder **refuses to sign**
  (:class:`CleanRunBoundaryError`) because the completeness claim would be
  vacuous;
* the activity set is drawn from the run's Merkle-chained
  :mod:`~bernstein.core.replay.journal` rows (head hash = run identity), and
  the attestation records that head so an omitted or mutated contaminating
  access breaks the anchor rather than silently trimming the set. The
  attestation binds the journal head only: the audit-chain mirror carries the
  attestation's own hash, so it cannot sit inside the sealed bytes and is
  verified separately (``bernstein audit verify`` plus
  :func:`project_clean_run_receipt`);
* :func:`scan_activity` is a pure set-membership pass over sealed digests:
  verdict is ``CLEAN`` iff zero contraband matches and zero out-of-scope
  accesses, and matches are recorded as ``(journal index, match class)``
  positions -- never plaintext;
* the canonical binding is anchored in the dedicated ``eval-clean-run``
  lineage-spine run and mirrored into the HMAC audit chain via
  :func:`~bernstein.core.security.audit_chain.record_clean_run_attestation`;
* :func:`verify_clean_run_attestation` re-derives the verdict from the
  embedded evidence, re-derives the sealed activity set from the anchored
  journal, and rejects any attestation whose stored verdict its evidence does
  not entail -- even when the attestation's own hashes are internally
  consistent. Strip the journal and the verdict collapses to an unanchored
  claim, which fails closed.

Scanner posture matches :mod:`bernstein.core.security.dlp_scanner`:
normalisation plus digest membership only -- no network, no LLM.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bernstein.core.lineage.spine import LineageSpine, content_hash_of
from bernstein.core.replay.journal import run_journal_path, verify_events

if TYPE_CHECKING:
    from collections.abc import Sequence

    from bernstein.core.security.audit_chain import AuditChainStore
    from bernstein.core.security.audit_receipt import AuditReceipt
    from bernstein.core.security.lineage_kms import KMSAdapter
    from bernstein.core.security.network_isolation import NetworkPolicy
    from bernstein.eval.golden import GoldenTask

logger = logging.getLogger(__name__)

#: Version stamped into every attestation. Bump only on a wire-format change.
CLEAN_RUN_SCHEMA_VERSION = 1

#: Lineage run id under which every clean-run attestation is anchored, kept
#: separate so attestations never interleave with per-task journals (the same
#: convention as ``eval-gate``).
EVAL_CLEAN_RUN_RUN_ID = "eval-clean-run"

#: Word-window size for content n-grams. A window shorter than this (but
#: non-empty) is sealed as a single whole-text window so short reference blobs
#: stay detectable.
NGRAM_WORDS = 8

#: Word-run lengths sealed for every observed activity span. Contraband tokens
#: up to this many words are matchable; longer tokens are additionally folded
#: into the n-gram space.
MAX_TOKEN_WORDS = 6

_CLEAN_RUN_ACTOR = "bernstein.eval_clean_run"
_CLEAN_RUN_SUBPATH = (".sdd", "eval", "clean_run")
_ATTESTATION_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SEAL_DOMAIN = "bernstein.eval.clean_run"

#: Characters preserved by normalisation; everything else becomes a space so a
#: token embedded in JSON tool arguments still tokenises to the same words as
#: the sealed contraband token.
_NORMALIZE_RE = re.compile(r"[^a-z0-9_./-]+")

#: Closed set of journal payload fields treated as scannable content spans.
_SPAN_FIELDS = ("content_window", "content", "arguments", "command")

#: Closed set of journal payload fields naming an accessed filesystem path.
_PATH_FIELDS = ("path", "file_path")


class CleanRunError(ValueError):
    """Base error for clean-run attestation failures."""


class CleanRunBoundaryError(CleanRunError):
    """No bounded worktree root: the in-scope universe is undefined.

    Raised instead of signing. A shared-workspace run has no closed set of
    in-scope reads, so a ``CLEAN`` claim over it would be vacuous.
    """


class CleanRunAnchorError(CleanRunError):
    """The activity set cannot be anchored (empty or broken journal chain)."""


class CleanRunCommitmentError(CleanRunError):
    """The contraband commitment cannot be built honestly.

    Raised instead of sealing, in two cases: declared reference content could
    not be loaded (a commitment that silently omitted material the task
    declared would let a contaminated run attest ``CLEAN`` against an
    incomplete ground-truth set), or caller-supplied extra material collides
    with a task-derived label (additive means strictly additive -- extra
    blobs may only add, never replace the task's own ground-truth).
    """


class CleanRunSchemaError(CleanRunError):
    """A stored attestation payload fails exact-type validation.

    Deserializers never coerce: a field signed as an int must be stored as
    an int (``bool`` is rejected explicitly as an int subclass), a string as
    a string. Coercion would let distinct stored bytes -- ``"7"`` or
    ``true`` where ``7`` was signed -- re-canonicalize to the same body hash
    and verify as the signed body.
    """


class CleanRunProjectionError(CleanRunError):
    """The projected chain range does not cover the attestation's mirror."""


class CleanRunVerdict(Enum):
    """Outcome of the deterministic contamination scan."""

    CLEAN = "clean"
    DIRTY = "dirty"


# ---------------------------------------------------------------------------
# Sealing helpers
# ---------------------------------------------------------------------------


def _normalize(text: str) -> str:
    """Lowercase and collapse everything outside ``[a-z0-9_./-]`` to spaces."""
    return _NORMALIZE_RE.sub(" ", text.lower()).strip()


def _seal(key: bytes, kind: str, value: str) -> str:
    """HMAC-SHA256 digest of *value* under *key*, domain-separated by *kind*."""
    payload = f"{_SEAL_DOMAIN}.{kind}.v1\x00{value}".encode()
    return _hmac.new(key, payload, hashlib.sha256).hexdigest()


def _windows(normalized: str, n: int = NGRAM_WORDS) -> list[str]:
    """Word n-gram windows of *normalized*; one whole-text window when short."""
    words = normalized.split()
    if not words:
        return []
    if len(words) < n:
        return [" ".join(words)]
    return [" ".join(words[i : i + n]) for i in range(len(words) - n + 1)]


def _word_runs(normalized: str, max_words: int = MAX_TOKEN_WORDS) -> list[str]:
    """Every contiguous word run of length 1..*max_words* in *normalized*."""
    words = normalized.split()
    runs: list[str] = []
    for length in range(1, min(max_words, len(words)) + 1):
        runs.extend(" ".join(words[i : i + length]) for i in range(len(words) - length + 1))
    return runs


def _hash_obj(obj: Any) -> str:
    payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------------------
# Exact-type payload readers (never coerce)
# ---------------------------------------------------------------------------


def _require(raw: Mapping[str, Any], field: str) -> Any:
    if not isinstance(raw, Mapping) or field not in raw:
        raise CleanRunSchemaError(f"missing field {field!r}")
    return raw[field]


def _require_int(raw: Mapping[str, Any], field: str) -> int:
    value = _require(raw, field)
    if type(value) is not int:
        raise CleanRunSchemaError(f"field {field!r} must be an int, got {type(value).__name__}")
    return value


def _require_str(raw: Mapping[str, Any], field: str) -> str:
    value = _require(raw, field)
    if type(value) is not str:
        raise CleanRunSchemaError(f"field {field!r} must be a str, got {type(value).__name__}")
    return value


def _optional_str(raw: Mapping[str, Any], field: str, default: str = "") -> str:
    if not isinstance(raw, Mapping) or field not in raw:
        return default
    value = raw[field]
    if type(value) is not str:
        raise CleanRunSchemaError(f"field {field!r} must be a str, got {type(value).__name__}")
    return value


def _require_str_items(raw: Mapping[str, Any], field: str) -> tuple[str, ...]:
    value = _require(raw, field)
    if type(value) is not list and type(value) is not tuple:
        raise CleanRunSchemaError(f"field {field!r} must be a list of strings")
    items: list[Any] = list(value)
    if any(type(item) is not str for item in items):
        raise CleanRunSchemaError(f"field {field!r} must contain only strings")
    return tuple(items)


def _require_record(raw: Mapping[str, Any], field: str) -> Mapping[str, Any]:
    value = _require(raw, field)
    if not isinstance(value, Mapping):
        raise CleanRunSchemaError(f"field {field!r} must be an object")
    return value


def _require_records(raw: Mapping[str, Any], field: str) -> list[Mapping[str, Any]]:
    value = _require(raw, field)
    if type(value) is not list and type(value) is not tuple:
        raise CleanRunSchemaError(f"field {field!r} must be a list of objects")
    records: list[Mapping[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise CleanRunSchemaError(f"field {field!r} must contain only objects")
        records.append(item)
    return records


# ---------------------------------------------------------------------------
# Contraband commitment
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ContrabandSet:
    """The task's ground-truth, sealed as keyed digests.

    Stores only HMAC digests plus non-identifying shape metadata (window
    sizes and coverage counts), so publishing the attestation can never
    reveal the solution. The ``salt_commitment`` binds the set to the
    operator key without revealing it.

    Coverage is explicit, never silent: ``token_source_count`` records how
    many task-derived token sources were committed and
    ``reference_source_count`` how many reference-content blobs fed the
    n-gram space. A task that genuinely has no reference content seals with
    ``reference_source_count == 0`` visible in the signed bytes, so a
    verifier can always see what the commitment covered.
    """

    schema_version: int
    salt_commitment: str
    token_digests: tuple[str, ...]
    ngram_digests: tuple[str, ...]
    ngram_words: int
    max_token_words: int
    token_source_count: int
    reference_source_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "salt_commitment": self.salt_commitment,
            "token_digests": list(self.token_digests),
            "ngram_digests": list(self.ngram_digests),
            "ngram_words": self.ngram_words,
            "max_token_words": self.max_token_words,
            "token_source_count": self.token_source_count,
            "reference_source_count": self.reference_source_count,
        }

    def canonical_bytes(self) -> bytes:
        """Canonical JSON bytes (sorted keys, minimal separators, UTF-8)."""
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ContrabandSet:
        """Parse with exact-type strictness; never coerce.

        Raises:
            CleanRunSchemaError: A field is missing or carries the wrong
                exact type (``bool`` is rejected where an int was signed).
        """
        return cls(
            schema_version=_require_int(raw, "schema_version"),
            salt_commitment=_require_str(raw, "salt_commitment"),
            token_digests=_require_str_items(raw, "token_digests"),
            ngram_digests=_require_str_items(raw, "ngram_digests"),
            ngram_words=_require_int(raw, "ngram_words"),
            max_token_words=_require_int(raw, "max_token_words"),
            token_source_count=_require_int(raw, "token_source_count"),
            reference_source_count=_require_int(raw, "reference_source_count"),
        )


def build_contraband_set(
    task: GoldenTask,
    *,
    key: bytes,
    reference_blobs: Mapping[str, str] | None = None,
) -> ContrabandSet:
    """Seal a :class:`~bernstein.eval.golden.GoldenTask`'s ground-truth.

    Tokens are the task-identifying strings (id, title, completion signals,
    expected test commands); n-grams are content windows over the
    reference-content blobs. The task *description* is what the agent is
    legitimately shown, so it is never contraband. Blob iteration is sorted by
    key and digest sets are sorted and de-duplicated, so two builds over the
    same inputs are byte-identical. Coverage counts are sealed alongside the
    digests so an empty n-gram space is a visible fact, never a silent one.

    The attestation builder derives the task's own golden-source material via
    :func:`derive_task_reference_blobs` before calling this, so *reference_blobs*
    is additive extra material -- omitting it cannot silently drop the task's
    derivable reference content from the commitment.

    Args:
        task: The golden task whose ground-truth to commit to.
        key: Operator audit HMAC key (no new key material).
        reference_blobs: Reference-content blobs keyed by a label; only the
            values are sealed (labels never enter the artifact).

    Returns:
        The sealed :class:`ContrabandSet`.
    """
    token_sources = [task.id, task.title, *task.completion_signals, *task.expected_test_outcomes]
    tokens: set[str] = set()
    ngrams: set[str] = set()
    token_source_count = 0
    reference_source_count = 0
    for source in token_sources:
        normalized = _normalize(source)
        if not normalized:
            continue
        token_source_count += 1
        tokens.add(_seal(key, "token", normalized))
        # A token longer than the matchable word-run cap is still catchable
        # through the n-gram space.
        if len(normalized.split()) > MAX_TOKEN_WORDS:
            ngrams.update(_seal(key, "ngram", w) for w in _windows(normalized))
    for _, blob in sorted((reference_blobs or {}).items()):
        normalized = _normalize(blob)
        if not normalized:
            continue
        reference_source_count += 1
        ngrams.update(_seal(key, "ngram", w) for w in _windows(normalized))
    return ContrabandSet(
        schema_version=CLEAN_RUN_SCHEMA_VERSION,
        salt_commitment=_seal(key, "salt", ""),
        token_digests=tuple(sorted(tokens)),
        ngram_digests=tuple(sorted(ngrams)),
        ngram_words=NGRAM_WORDS,
        max_token_words=MAX_TOKEN_WORDS,
        token_source_count=token_source_count,
        reference_source_count=reference_source_count,
    )


def _frontmatter_of(text: str) -> str | None:
    """Return the YAML frontmatter of a golden markdown source, or ``None``.

    Mirrors the ``---`` split contract of :mod:`bernstein.eval.golden`: the
    frontmatter carries the hidden ground-truth strings (completion signals,
    expected outcomes), while the body is the description the agent is
    legitimately shown -- only the frontmatter is ever committed.
    """
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    return parts[1].strip()


def derive_task_reference_blobs(
    task: GoldenTask,
    *,
    golden_dir: Path | None = None,
) -> dict[str, str]:
    """Reference material derivable from the task's own golden source.

    Locates the task's golden markdown (operator override under
    ``<golden_dir>/<tier>/*.md`` first, then the packaged
    ``bernstein.eval.golden_data`` fixture) by parsed task id and returns its
    YAML frontmatter text -- the part carrying the hidden ground-truth
    strings -- keyed by an internal label that never enters the artifact.
    The description body is excluded: it is what the agent is legitimately
    shown.

    A synthetic task backed by no golden source (and no *golden_dir*
    declaration) derives nothing; the commitment then seals
    ``reference_source_count == 0`` visibly rather than pretending coverage.

    Args:
        task: The task whose golden source to locate (matched by ``id``
            within ``task.tier``).
        golden_dir: Operator golden root. Supplying it *declares* that the
            task's source must be loadable.

    Returns:
        A blob mapping suitable for :func:`build_contraband_set`.

    Raises:
        CleanRunCommitmentError: *golden_dir* was supplied but the task's
            golden source could not be located there or in the packaged
            data -- declared reference content must never go silently
            uncommitted.
    """
    from bernstein.eval.golden import _packaged_tier_files, _parse_golden_text

    label = f"golden:{task.id}"
    if golden_dir is not None:
        tier_dir = golden_dir / task.tier
        if tier_dir.is_dir():
            for md_file in sorted(tier_dir.glob("*.md")):
                try:
                    text = md_file.read_text(encoding="utf-8")
                except OSError:
                    continue
                parsed = _parse_golden_text(text, task.tier, str(md_file))
                frontmatter = _frontmatter_of(text)
                if parsed is not None and parsed.id == task.id and frontmatter:
                    return {label: frontmatter}
    for entry in _packaged_tier_files(task.tier):
        try:
            text = entry.read_text(encoding="utf-8")
        except OSError:
            continue
        parsed = _parse_golden_text(text, task.tier, entry.name)
        frontmatter = _frontmatter_of(text)
        if parsed is not None and parsed.id == task.id and frontmatter:
            return {label: frontmatter}
    if golden_dir is not None:
        raise CleanRunCommitmentError(
            f"refusing to seal: golden_dir {golden_dir} was declared but no golden source "
            f"for task {task.id!r} (tier {task.tier!r}) could be loaded from it or from the "
            "packaged data -- declared reference content must not go silently uncommitted",
        )
    return {}


# ---------------------------------------------------------------------------
# Scope boundary
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScopeBoundary:
    """The closed universe of in-scope reads, taken from the substrate.

    ``worktree_root`` is the resolved task worktree directory;
    ``allowed_endpoints`` are the ``host:port`` spellings of the network
    policy's allowlist. Anything outside either is an out-of-scope access.
    """

    worktree_root: str
    allowed_endpoints: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "worktree_root": self.worktree_root,
            "allowed_endpoints": list(self.allowed_endpoints),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ScopeBoundary:
        """Parse with exact-type strictness; never coerce.

        Raises:
            CleanRunSchemaError: A field is missing or mistyped.
        """
        return cls(
            worktree_root=_require_str(raw, "worktree_root"),
            allowed_endpoints=_require_str_items(raw, "allowed_endpoints"),
        )


def scope_boundary(worktree_root: Path | None, network_policy: NetworkPolicy) -> ScopeBoundary:
    """Build the scope boundary, refusing when no worktree boundary exists.

    Args:
        worktree_root: The task's worktree directory. ``None`` (shared
            workspace) or a non-directory refuses.
        network_policy: The sandbox network policy whose ``allowed_endpoints``
            define in-scope egress.

    Raises:
        CleanRunBoundaryError: The worktree boundary is absent, so the
            "closed universe of in-scope reads" is undefined and no
            attestation may be signed.
    """
    if worktree_root is None:
        raise CleanRunBoundaryError(
            "refusing to sign: no worktree root was supplied (shared-workspace mode), "
            "so the closed universe of in-scope reads is undefined",
        )
    if not worktree_root.is_dir():
        raise CleanRunBoundaryError(
            f"refusing to sign: worktree root {worktree_root} is not a directory, "
            "so the closed universe of in-scope reads is undefined",
        )
    endpoints = tuple(sorted(f"{e.host}:{e.port}" for e in network_policy.allowed_endpoints))
    return ScopeBoundary(
        worktree_root=os.path.realpath(worktree_root),
        allowed_endpoints=endpoints,
    )


# ---------------------------------------------------------------------------
# Activity extraction + scan
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ActivityRecord:
    """One journaled access, sealed for embedding in the attestation.

    ``span_digests`` are the keyed digests of every word run (token space) and
    n-gram window (n-gram space) of the row's scannable spans -- membership
    against the :class:`ContrabandSet` is a pure set intersection.
    ``scope_violation`` is ``""``, ``"path"`` or ``"endpoint"``.
    """

    index: int
    kind: str
    path_digest: str
    scope_violation: str
    span_digests: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "kind": self.kind,
            "path_digest": self.path_digest,
            "scope_violation": self.scope_violation,
            "span_digests": list(self.span_digests),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ActivityRecord:
        """Parse with exact-type strictness; never coerce.

        Raises:
            CleanRunSchemaError: A field is missing or mistyped (``bool`` is
                rejected where the journal index int was signed).
        """
        return cls(
            index=_require_int(raw, "index"),
            kind=_require_str(raw, "kind"),
            path_digest=_require_str(raw, "path_digest"),
            scope_violation=_require_str(raw, "scope_violation"),
            span_digests=_require_str_items(raw, "span_digests"),
        )


@dataclass(frozen=True, slots=True)
class ScanMatch:
    """One contamination finding: a journal position and a match class."""

    index: int
    match_class: str

    def to_dict(self) -> dict[str, Any]:
        return {"index": self.index, "match_class": self.match_class}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ScanMatch:
        """Parse with exact-type strictness; never coerce.

        Raises:
            CleanRunSchemaError: A field is missing or mistyped.
        """
        return cls(index=_require_int(raw, "index"), match_class=_require_str(raw, "match_class"))


def _span_of(value: Any) -> str:
    """Project a journal payload value onto a scannable string span."""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)


def _path_in_scope(raw_path: str, worktree_root: str) -> bool:
    """Whether *raw_path* (relative paths anchor at the root) stays inside."""
    candidate = raw_path if os.path.isabs(raw_path) else os.path.join(worktree_root, raw_path)
    resolved = os.path.realpath(candidate)
    root = os.path.realpath(worktree_root)
    try:
        return os.path.commonpath([root, resolved]) == root
    except ValueError:
        return False


def extract_activity(
    events: Sequence[Mapping[str, Any]],
    *,
    boundary: ScopeBoundary,
    key: bytes,
) -> tuple[ActivityRecord, ...]:
    """Project journal rows onto sealed :class:`ActivityRecord`s.

    A row participates when it carries a path field (``path`` /
    ``file_path``), a network field (``endpoint`` or ``host``+``port``), or a
    content span field (``content_window`` / ``content`` / ``arguments`` /
    ``command``); all other rows (scheduling events) are skipped. The record's
    ``index`` is the row's position in the chained journal, so a match names a
    precise, anchored step.
    """
    records: list[ActivityRecord] = []
    for index, row in enumerate(events):
        raw_path = next((str(row[f]) for f in _PATH_FIELDS if isinstance(row.get(f), str) and row[f]), "")
        endpoint = ""
        if isinstance(row.get("endpoint"), str) and row["endpoint"]:
            endpoint = str(row["endpoint"])
        elif row.get("host") is not None and row.get("port") is not None:
            endpoint = f"{row['host']}:{row['port']}"
        spans = [_span_of(row[f]) for f in _SPAN_FIELDS if row.get(f) is not None]
        if raw_path:
            spans.append(raw_path)
        if endpoint:
            spans.append(endpoint)
        if not spans:
            continue

        scope_violation = ""
        if raw_path and not _path_in_scope(raw_path, boundary.worktree_root):
            scope_violation = "path"
        elif endpoint and endpoint not in boundary.allowed_endpoints:
            scope_violation = "endpoint"

        digests: set[str] = set()
        for span in spans:
            normalized = _normalize(span)
            if not normalized:
                continue
            digests.update(_seal(key, "token", run) for run in _word_runs(normalized))
            digests.update(_seal(key, "ngram", w) for w in _windows(normalized))
        records.append(
            ActivityRecord(
                index=index,
                kind=str(row.get("event", "")),
                path_digest=_seal(key, "path", _normalize(raw_path)) if raw_path else "",
                scope_violation=scope_violation,
                span_digests=tuple(sorted(digests)),
            )
        )
    return tuple(records)


def scan_activity(
    activities: Sequence[ActivityRecord],
    contraband: ContrabandSet,
) -> tuple[ScanMatch, ...]:
    """Pure membership scan of sealed activity against the sealed commitment.

    Deterministic and keyless: it sees only digests, so a verifier re-runs it
    from the embedded evidence alone. At most one match per class per
    activity, ordered by journal index then class.
    """
    tokens = frozenset(contraband.token_digests)
    ngrams = frozenset(contraband.ngram_digests)
    matches: list[ScanMatch] = []
    for activity in activities:
        spans = frozenset(activity.span_digests)
        if spans & tokens:
            matches.append(ScanMatch(index=activity.index, match_class="contraband_token"))
        if spans & ngrams:
            matches.append(ScanMatch(index=activity.index, match_class="contraband_ngram"))
        if activity.scope_violation == "path":
            matches.append(ScanMatch(index=activity.index, match_class="out_of_scope_path"))
        elif activity.scope_violation == "endpoint":
            matches.append(ScanMatch(index=activity.index, match_class="out_of_scope_endpoint"))
    return tuple(matches)


def derive_verdict(matches: Sequence[ScanMatch]) -> CleanRunVerdict:
    """``CLEAN`` iff zero contraband matches and zero out-of-scope accesses."""
    return CleanRunVerdict.CLEAN if not matches else CleanRunVerdict.DIRTY


# ---------------------------------------------------------------------------
# Attestation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CleanRunAttestation:
    """A sealed clean-run attestation.

    The body (everything ``attestation_hash`` covers) binds the contraband
    commitment, the scope roots, the sealed activity set, the journal-head
    anchor, the verdict, the match positions, the schema version, and the
    timestamp. ``journal_entry_hash`` is the lineage-spine anchor assigned
    post-seal and is not part of the hashed body.

    The attestation binds the **journal head only**. The audit-chain mirror
    (:func:`~bernstein.core.security.audit_chain.record_clean_run_attestation`)
    carries this attestation's hash, so by construction it cannot sit inside
    the sealed bytes; it is verified separately -- a tampered mirror fails
    ``bernstein audit verify`` like any tampered chain entry, and
    :func:`project_clean_run_receipt` refuses to project a chain range that
    does not cover it.
    """

    schema_version: int
    run_id: str
    task_commitment: str
    contraband: ContrabandSet
    scope: ScopeBoundary
    activities: tuple[ActivityRecord, ...]
    journal_head: str
    verdict: str
    matches: tuple[ScanMatch, ...]
    timestamp: int
    attestation_hash: str
    journal_entry_hash: str = ""

    def body(self) -> dict[str, Any]:
        """The hashed body: every field except the hash and the anchor."""
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "task_commitment": self.task_commitment,
            "contraband": self.contraband.to_dict(),
            "scope": self.scope.to_dict(),
            "activities": [a.to_dict() for a in self.activities],
            "journal_head": self.journal_head,
            "verdict": self.verdict,
            "matches": [m.to_dict() for m in self.matches],
            "timestamp": self.timestamp,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self.body()
        payload["attestation_hash"] = self.attestation_hash
        payload["journal_entry_hash"] = self.journal_entry_hash
        return payload

    def canonical_bytes(self) -> bytes:
        """Canonical bytes sealed into the lineage spine (body + hash).

        The spine anchor is excluded: it is the one field assigned by the
        seal itself, so the cross-machine byte-equality contract covers
        everything else.
        """
        payload = self.body()
        payload["attestation_hash"] = self.attestation_hash
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> CleanRunAttestation:
        """Parse with exact-type strictness; never coerce.

        Coercion here would break byte-honesty: ``"7"`` or ``true`` stored
        where ``7`` was signed would re-canonicalize to the same body hash
        and pass verification, so distinct stored bytes would verify as one
        signed body.

        Raises:
            CleanRunSchemaError: A field is missing or carries the wrong
                exact type (``bool`` is rejected as an int subclass).
        """
        return cls(
            schema_version=_require_int(raw, "schema_version"),
            run_id=_require_str(raw, "run_id"),
            task_commitment=_require_str(raw, "task_commitment"),
            contraband=ContrabandSet.from_dict(_require_record(raw, "contraband")),
            scope=ScopeBoundary.from_dict(_require_record(raw, "scope")),
            activities=tuple(ActivityRecord.from_dict(a) for a in _require_records(raw, "activities")),
            journal_head=_require_str(raw, "journal_head"),
            verdict=_require_str(raw, "verdict"),
            matches=tuple(ScanMatch.from_dict(m) for m in _require_records(raw, "matches")),
            timestamp=_require_int(raw, "timestamp"),
            attestation_hash=_require_str(raw, "attestation_hash"),
            journal_entry_hash=_optional_str(raw, "journal_entry_hash"),
        )


def _refuse_unprobeable_or_linked(probe: Path) -> None:
    """Fail-closed link probe for one clean-run store component.

    Deliberately not ``is_filesystem_link``: that shared helper answers
    ``False`` when the probe itself fails (a best-effort contract that
    serves the worktree GC sweep), and a store walk that cannot prove a
    component is not a link must refuse rather than continue. A component
    that does not exist yet is fine -- ``is_symlink`` / ``is_junction``
    return ``False`` without raising for a missing path -- so sealing into
    a fresh workdir still creates the store.

    Raises:
        ValueError: The component is a symlink or junction, or the probe
            itself failed.
    """
    try:
        linked = probe.is_symlink()
        if not linked:
            probe_junction = getattr(probe, "is_junction", None)
            linked = probe_junction is not None and bool(probe_junction())
    except OSError as exc:
        msg = f"clean-run attestation store component could not be probed for links; refusing: {probe}: {exc.errno}"
        raise ValueError(msg) from exc
    if linked:
        msg = f"clean-run attestation store path is a symlink or junction; refusing to follow it: {probe}"
        raise ValueError(msg)


def _read_leaf_text(path: Path) -> str:
    """Read one attestation leaf without following a symlink planted there.

    Opens with ``O_NOFOLLOW`` so a symlink swapped in at the receipt
    filename after path validation is rejected atomically by the read
    itself -- a separate pre-check would leave a TOCTOU window. Mirrors the
    CAS blob read (:mod:`bernstein.core.persistence.cas_store`). The flag
    is POSIX-only; where it is absent it degrades to 0. A symlinked leaf
    surfaces as ``OSError`` (``ELOOP``), which callers classify as
    unreadable, never parsed.
    """
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "r", encoding="utf-8") as handle:
        return handle.read()


def clean_run_attestation_path(workdir: Path, attestation_hash: str) -> Path:
    """Return the on-disk attestation path for *attestation_hash*.

    The hash is validated against ``sha256:<64 hex>`` and the resolved path
    is asserted to stay under the clean-run directory (path-injection defense
    in depth, mirroring :func:`bernstein.eval.gate_receipt.verdict_receipt_path`).
    An attestation store relocated via a filesystem link is refused outright:
    with ``.sdd``, ``.sdd/eval``, or the clean-run directory itself symlinked
    (or, on Windows, junctioned -- ``Path.is_symlink()`` is ``False`` for
    NTFS junctions) elsewhere, base and candidate both resolve into the
    link's target and a realpath containment check passes vacuously, so the
    store would follow attacker-placed content. The probe fails closed: a
    component that cannot be probed for links refuses by name rather than
    continuing (the shared best-effort helper's ``False``-on-error contract
    serves the worktree GC sweep, not a store walk). Same posture as the
    gate and verifier-ladder receipt stores (#3414). The returned path is
    the *resolved* candidate. Directory-component races are accepted; leaf
    opens are no-follow per :mod:`bernstein.core.persistence.cas_store`.

    Raises:
        ValueError: The hash is not a canonical ``sha256:`` digest, a
            component of the clean-run directory is a symlink or junction or
            could not be probed, or the resolved path escapes the clean-run
            directory.
    """
    if not _ATTESTATION_HASH_RE.match(attestation_hash):
        msg = f"attestation_hash is not a canonical sha256 digest: {attestation_hash!r}"
        raise ValueError(msg)
    probe = workdir
    for part in _CLEAN_RUN_SUBPATH:
        probe = probe / part
        _refuse_unprobeable_or_linked(probe)
    base = workdir.joinpath(*_CLEAN_RUN_SUBPATH)
    candidate = base / f"{attestation_hash}.json"
    base_real = os.path.realpath(base)
    cand_real = os.path.realpath(candidate)
    if os.path.commonpath([base_real, cand_real]) != base_real:
        msg = f"attestation path escapes clean-run directory: {attestation_hash!r}"
        raise ValueError(msg)
    return Path(cand_real)


def recompute_attestation_hash(payload: Mapping[str, Any]) -> str:
    """Recompute ``attestation_hash`` from a stored attestation dict's body.

    Exposed so a verifier (or a test) can confirm that an attestation whose
    hashes are internally consistent is still rejected when its evidence does
    not entail its verdict. Scalar fields are read with the same exact-type
    strictness as :meth:`CleanRunAttestation.from_dict` -- a hash is never
    recomputed over coerced spellings of the signed body.

    Raises:
        CleanRunSchemaError: A scalar field is missing or mistyped.
    """
    body = {
        "schema_version": _require_int(payload, "schema_version"),
        "run_id": _require_str(payload, "run_id"),
        "task_commitment": _require_str(payload, "task_commitment"),
        "contraband": _require(payload, "contraband"),
        "scope": _require(payload, "scope"),
        "activities": _require(payload, "activities"),
        "journal_head": _require_str(payload, "journal_head"),
        "verdict": _require_str(payload, "verdict"),
        "matches": _require(payload, "matches"),
        "timestamp": _require_int(payload, "timestamp"),
    }
    return _hash_obj(body)


def _journal_head_of(events: Sequence[Mapping[str, Any]]) -> str:
    """The Merkle head of chained journal rows, refusing a broken chain."""
    if not events:
        raise CleanRunAnchorError(
            "refusing to attest: the run journal is empty, so there is no anchored activity set",
        )
    result = verify_events([dict(e) for e in events])
    if not result.ok:
        detail = "; ".join(result.errors) or "chain verification failed"
        raise CleanRunAnchorError(f"refusing to attest: the run journal does not chain ({detail})")
    return str(events[-1].get("event_hash", ""))


def build_clean_run_attestation(
    *,
    task: GoldenTask,
    journal_events: Sequence[Mapping[str, Any]],
    run_id: str,
    worktree_root: Path | None,
    network_policy: NetworkPolicy,
    workdir: Path,
    lineage_root: Path,
    hmac_key: bytes,
    timestamp: int,
    reference_blobs: Mapping[str, str] | None = None,
    golden_dir: Path | None = None,
    chain: AuditChainStore | None = None,
) -> CleanRunAttestation:
    """Scan a run's anchored activity and seal the verdict into an attestation.

    The boundary check runs first and fails closed: without a bounded
    worktree root nothing is scanned, nothing is written, and nothing is
    signed. The journal rows must form an intact Merkle chain; their head is
    recorded so the activity set cannot be silently trimmed after the fact.
    The attestation binds the journal head only -- the audit-chain mirror
    carries this attestation's hash, so it cannot sit inside the sealed
    bytes; it is verified separately by ``bernstein audit verify`` and by
    :func:`project_clean_run_receipt`.

    The contraband commitment always includes the reference material
    derivable from the task itself (:func:`derive_task_reference_blobs`);
    *reference_blobs* is additive extra material, so omitting it cannot
    silently weaken the commitment.

    Args:
        task: The golden task whose ground-truth is committed.
        journal_events: The run's chained journal rows in append order.
        run_id: The run identifier (names the journal for later verifiers).
        worktree_root: The task's worktree directory; ``None`` refuses.
        network_policy: Sandbox network policy defining in-scope egress.
        workdir: Project root (attestation written under
            ``.sdd/eval/clean_run``).
        lineage_root: ``.sdd/lineage`` root for the spine.
        hmac_key: Operator audit HMAC key (seals digests and the spine entry).
        timestamp: Injected integer timestamp (no wall-clock in signed bytes).
        reference_blobs: Additional reference-content blobs for the
            commitment, merged over the task-derived material.
        golden_dir: Operator golden root for the task-source lookup;
            supplying it declares the source must be loadable.
        chain: Optional :class:`AuditChainStore` accepting the mirror.

    Returns:
        The sealed :class:`CleanRunAttestation`.

    Raises:
        CleanRunBoundaryError: No bounded worktree root.
        CleanRunAnchorError: Empty or non-chaining journal rows.
        CleanRunCommitmentError: Declared reference content could not be
            loaded, or caller-supplied extra material collides with a
            task-derived label.
        CleanRunError: An attestation with this hash already exists in the
            store -- the store is write-once, so a duplicate seal (or a
            same-named planted leaf) refuses rather than overwriting.
    """
    boundary = scope_boundary(worktree_root, network_policy)
    journal_head = _journal_head_of(journal_events)

    derived_blobs = derive_task_reference_blobs(task, golden_dir=golden_dir)
    extra_blobs = dict(reference_blobs or {})
    colliding = sorted(set(derived_blobs) & set(extra_blobs))
    if colliding:
        raise CleanRunCommitmentError(
            f"refusing to seal: caller-supplied reference material collides with the "
            f"task-derived ground-truth label(s) {colliding} -- extra material may only "
            "add to the commitment, never replace what the task itself derives",
        )
    merged_blobs = {**derived_blobs, **extra_blobs}
    contraband = build_contraband_set(task, key=hmac_key, reference_blobs=merged_blobs)
    activities = extract_activity(journal_events, boundary=boundary, key=hmac_key)
    matches = scan_activity(activities, contraband)
    verdict = derive_verdict(matches)

    unsealed = CleanRunAttestation(
        schema_version=CLEAN_RUN_SCHEMA_VERSION,
        run_id=run_id,
        task_commitment=_seal(hmac_key, "token", _normalize(task.id)),
        contraband=contraband,
        scope=boundary,
        activities=activities,
        journal_head=journal_head,
        verdict=verdict.value,
        matches=matches,
        timestamp=timestamp,
        attestation_hash="",
    )
    attestation_hash = _hash_obj(unsealed.body())
    sealed_no_anchor = CleanRunAttestation(
        **{**_fields_of(unsealed), "attestation_hash": attestation_hash},
    )

    # The store is write-once: names are content-addressed, so a same-hash
    # leaf can only be a duplicate seal or a planted entry -- refuse before
    # the spine append so a refused seal leaves no dangling anchor.
    path = clean_run_attestation_path(workdir, attestation_hash)
    if os.path.lexists(path):
        msg = (
            f"refusing to seal: attestation {attestation_hash!r} already exists in the "
            "clean-run store (the store is write-once; a same-named leaf planted in the "
            "store also lands here)"
        )
        raise CleanRunError(msg)

    spine = LineageSpine(lineage_root, run_id=EVAL_CLEAN_RUN_RUN_ID, hmac_key=hmac_key)
    artifact_path = "/".join((*_CLEAN_RUN_SUBPATH, f"{attestation_hash}.json"))
    anchor = spine.record(
        artifact_path=artifact_path,
        content=sealed_no_anchor.canonical_bytes(),
        actor=_CLEAN_RUN_ACTOR,
        step_id=attestation_hash,
        model="",
        timestamp=timestamp,
    )
    sealed = CleanRunAttestation(
        **{**_fields_of(sealed_no_anchor), "journal_entry_hash": anchor},
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    # O_EXCL closes the leaf-swap race on create (a symlink or file swapped
    # in after the write-once check fails the open with EEXIST rather than
    # being followed or overwritten); O_NOFOLLOW-degrade mirrors the CAS
    # blob open (bernstein.core.persistence.cas_store). 0o600 -- the
    # attestation is a signed audit artifact, operator-only readable like
    # the lineage log (bernstein.core.lineage.store).
    write_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, write_flags, 0o600)
    except FileExistsError as exc:
        msg = (
            f"refusing to seal: attestation {attestation_hash!r} appeared in the "
            "clean-run store during sealing (the store is write-once)"
        )
        raise CleanRunError(msg) from exc
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(sealed.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True))

    if chain is not None:
        from bernstein.core.security.audit_chain import record_clean_run_attestation

        record_clean_run_attestation(
            chain=chain,
            run_id=run_id,
            attestation_hash=attestation_hash,
            verdict=verdict.value,
            task_commitment=sealed.task_commitment,
            journal_head=journal_head,
            journal_entry_hash=anchor,
        )
    return sealed


def _fields_of(attestation: CleanRunAttestation) -> dict[str, Any]:
    """Field mapping for rebuilding a frozen attestation with overrides."""
    return {
        "schema_version": attestation.schema_version,
        "run_id": attestation.run_id,
        "task_commitment": attestation.task_commitment,
        "contraband": attestation.contraband,
        "scope": attestation.scope,
        "activities": attestation.activities,
        "journal_head": attestation.journal_head,
        "verdict": attestation.verdict,
        "matches": attestation.matches,
        "timestamp": attestation.timestamp,
        "attestation_hash": attestation.attestation_hash,
        "journal_entry_hash": attestation.journal_entry_hash,
    }


def read_clean_run_attestation(workdir: Path, attestation_hash: str) -> CleanRunAttestation | None:
    """Return the stored attestation for *attestation_hash*, or ``None``."""
    try:
        path = clean_run_attestation_path(workdir, attestation_hash)
    except ValueError:
        return None
    try:
        raw = _read_leaf_text(path)
    except FileNotFoundError:
        return None
    except OSError:
        logger.warning("eval: clean-run attestation leaf refused a no-follow open at %s", path)
        return None
    try:
        return CleanRunAttestation.from_dict(json.loads(raw))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        logger.warning("eval: malformed clean-run attestation at %s", path)
        return None


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CleanRunVerifyResult:
    """Outcome of an offline clean-run attestation verification."""

    ok: bool
    reason: str
    attestation: CleanRunAttestation | None


def verify_clean_run_attestation(
    *,
    workdir: Path,
    lineage_root: Path,
    hmac_key: bytes,
    attestation_hash: str,
    journal_events: Sequence[Mapping[str, Any]] | None = None,
) -> CleanRunVerifyResult:
    """Re-verify the attestation for *attestation_hash* offline.

    The stored verdict is never trusted. In order:

    * the stored bytes parse under exact-type strictness
      (:class:`CleanRunSchemaError` otherwise) -- a signed int cannot be
      re-spelled as its string or as a bool and still verify;
    * the attestation hash recomputes from the stored body;
    * the contraband commitment is bound to the operator key (salt
      commitment recomputes);
    * the verdict and match positions re-derive from the *embedded* activity
      digests and contraband commitment alone (keyless set membership), so a
      stored-``CLEAN`` attestation whose embedded activity contains a
      contraband match is rejected even with internally consistent hashes;
    * the journal rows (supplied, or loaded from
      ``.sdd/runs/<run_id>/journal.jsonl`` under *workdir*) must form an
      intact chain whose head equals the recorded ``journal_head`` -- an
      activity set that does not chain to the recorded head is rejected as
      unanchored, and a missing journal fails closed the same way;
    * the sealed activity set re-derives from those anchored rows under the
      key and must equal the embedded set, so a trimmed embedded activity
      list cannot ride an intact anchor; and
    * the lineage spine verifies and anchors the attestation's canonical
      bytes at the recorded entry hash.
    """
    try:
        path = clean_run_attestation_path(workdir, attestation_hash)
    except ValueError as exc:
        # Surfaces the named refusal verbatim: a non-canonical hash or a
        # store component that is a symlink/junction (refused, not followed).
        return CleanRunVerifyResult(ok=False, reason=str(exc), attestation=None)
    try:
        raw = _read_leaf_text(path)
    except FileNotFoundError:
        return CleanRunVerifyResult(ok=False, reason=f"no attestation for {attestation_hash!r}", attestation=None)
    except OSError as exc:
        return CleanRunVerifyResult(
            ok=False,
            reason=f"attestation leaf could not be opened without following links; refusing: {exc}",
            attestation=None,
        )
    try:
        attestation = CleanRunAttestation.from_dict(json.loads(raw))
    except json.JSONDecodeError:
        return CleanRunVerifyResult(ok=False, reason="stored attestation is not valid JSON", attestation=None)
    except CleanRunSchemaError as exc:
        return CleanRunVerifyResult(ok=False, reason=f"stored attestation is schema-invalid: {exc}", attestation=None)
    if attestation.attestation_hash != attestation_hash:
        return CleanRunVerifyResult(ok=False, reason="attestation hash does not match request", attestation=attestation)

    if _hash_obj(attestation.body()) != attestation.attestation_hash:
        return CleanRunVerifyResult(
            ok=False,
            reason="attestation_hash does not recompute from the stored body (tampered)",
            attestation=attestation,
        )

    if attestation.contraband.salt_commitment != _seal(hmac_key, "salt", ""):
        return CleanRunVerifyResult(
            ok=False,
            reason="contraband commitment is not bound to the operator key",
            attestation=attestation,
        )

    rederived_matches = scan_activity(attestation.activities, attestation.contraband)
    rederived_verdict = derive_verdict(rederived_matches)
    if rederived_matches != attestation.matches or rederived_verdict.value != attestation.verdict:
        return CleanRunVerifyResult(
            ok=False,
            reason="stored evidence does not entail its verdict (re-derivation mismatch)",
            attestation=attestation,
        )

    events = list(journal_events) if journal_events is not None else _load_run_journal(workdir, attestation.run_id)
    if not events:
        return CleanRunVerifyResult(
            ok=False,
            reason="run journal unavailable: the activity set is unanchored",
            attestation=attestation,
        )
    chain_result = verify_events([dict(e) for e in events])
    if not chain_result.ok or str(events[-1].get("event_hash", "")) != attestation.journal_head:
        return CleanRunVerifyResult(
            ok=False,
            reason="activity set does not chain to the recorded journal head (unanchored)",
            attestation=attestation,
        )

    rederived_activity = extract_activity(events, boundary=attestation.scope, key=hmac_key)
    if rederived_activity != attestation.activities:
        return CleanRunVerifyResult(
            ok=False,
            reason="embedded activity set does not derive from the anchored journal",
            attestation=attestation,
        )

    spine = LineageSpine(lineage_root, run_id=EVAL_CLEAN_RUN_RUN_ID, hmac_key=hmac_key)
    report = spine.verify()
    if not report.ok:
        detail = "; ".join(report.errors) if report.errors else report.status.value
        return CleanRunVerifyResult(
            ok=False,
            reason=f"eval-clean-run spine failed verification: {detail}",
            attestation=attestation,
        )
    expected_content = content_hash_of(attestation.canonical_bytes())
    anchored = any(
        entry.entry_hash == attestation.journal_entry_hash and entry.content_hash == expected_content
        for entry in spine.iter_entries()
    )
    if not anchored:
        return CleanRunVerifyResult(
            ok=False,
            reason="attestation is not anchored in the eval-clean-run spine",
            attestation=attestation,
        )
    return CleanRunVerifyResult(ok=True, reason="", attestation=attestation)


def _load_run_journal(workdir: Path, run_id: str) -> list[dict[str, Any]]:
    """Load the run's journal rows from disk; empty when unavailable."""
    from bernstein.core.replay.journal import JournalPathError, load_events

    try:
        path = run_journal_path(workdir / ".sdd", run_id)
    except JournalPathError:
        return []
    return load_events(path)


# ---------------------------------------------------------------------------
# Offline receipt projection
# ---------------------------------------------------------------------------


def project_clean_run_receipt(
    audit_dir: Path,
    *,
    attestation_hash: str,
    since: str,
    until: str,
    key: bytes,
    kms_adapter: KMSAdapter,
    output_dir: Path | None = None,
    write: bool = True,
) -> AuditReceipt:
    """Project the chain range covering the attestation's mirror into receipts.

    Reuses :func:`bernstein.core.security.audit_receipt.build_receipt` (COSE /
    in-toto / transparency; same KMS adapter, no new key material). The
    projected range must contain the ``eval.clean_run_attestation`` event for
    *attestation_hash*: the subject digest is the range head, so the signed
    envelopes cover the mirrored attestation identity and a verifier holding
    neither the plaintext ground-truth nor the HMAC key can check them.

    Raises:
        CleanRunProjectionError: The range holds no mirror event for
            *attestation_hash*, so the receipt would not cover it.
    """
    from bernstein.core.security.audit_chain import EVENT_CLEAN_RUN_ATTESTATION
    from bernstein.core.security.audit_receipt import build_receipt

    receipt = build_receipt(
        audit_dir,
        since=since,
        until=until,
        key=key,
        kms_adapter=kms_adapter,
        subject_name=f"clean-run-attestation-{attestation_hash}",
        output_dir=output_dir,
        write=write,
    )
    events: list[dict[str, Any]] = receipt.receipt.get("events", [])
    covered = any(
        e.get("event_type") == EVENT_CLEAN_RUN_ATTESTATION
        and e.get("details", {}).get("attestation_hash") == attestation_hash
        for e in events
    )
    if not covered:
        raise CleanRunProjectionError(
            f"projected range {since}..{until} carries no clean-run attestation mirror for {attestation_hash!r}",
        )
    return receipt


__all__ = [
    "CLEAN_RUN_SCHEMA_VERSION",
    "EVAL_CLEAN_RUN_RUN_ID",
    "MAX_TOKEN_WORDS",
    "NGRAM_WORDS",
    "ActivityRecord",
    "CleanRunAnchorError",
    "CleanRunAttestation",
    "CleanRunBoundaryError",
    "CleanRunCommitmentError",
    "CleanRunError",
    "CleanRunProjectionError",
    "CleanRunSchemaError",
    "CleanRunVerdict",
    "CleanRunVerifyResult",
    "ContrabandSet",
    "ScanMatch",
    "ScopeBoundary",
    "build_clean_run_attestation",
    "build_contraband_set",
    "clean_run_attestation_path",
    "derive_task_reference_blobs",
    "derive_verdict",
    "extract_activity",
    "project_clean_run_receipt",
    "read_clean_run_attestation",
    "recompute_attestation_hash",
    "scan_activity",
    "scope_boundary",
    "verify_clean_run_attestation",
]
