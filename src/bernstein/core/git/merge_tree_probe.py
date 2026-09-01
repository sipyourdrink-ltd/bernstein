"""Deterministic pair-wise integration probe over two live worker commits.

The merge machinery in :mod:`bernstein.core.git.merge_queue` answers one
question -- "would this finished branch land on the base" -- and answers it on
the gate path, after the agent has already spent its entire run.  Two workers
that were each individually mergeable when they were admitted can stop
composing with *each other* ten commits later, and today the first evidence of
that is a conflict raised at merge-back, at which point the cheapest available
action is to discard one of the runs.

This module supplies the primitive that closes that interval: an ordered pair
of live worker commits in, a content-addressed statement of what they compose
to out::

    probe = probe_integration(worker_a_head, worker_b_head, repo_root)
    probe.tree_id      # the merge, named by its content
    probe.verdict      # TEXTUAL_CLEAN | CONFLICTED | UNAVAILABLE

Why the tree id is the point
---------------------------
``git merge-tree --write-tree`` does not return a *report* about a merge; it
returns the merged tree object itself, named by its content.  Two parties who
probe the same pair either produce the same object id or do not have the same
repository.  That is what makes the result worth signing later (issue #3279
step 3): a third party holding the repository and stock git re-derives it with
no bernstein-shaped step in the middle of the argument::

    git merge-tree --write-tree --name-only -z <a_commit> <b_commit>

``-z`` is deliberate.  Without it git C-quotes any path containing a space, a
quote, or a non-ASCII byte, so a recorded digest would cover the quoted form
and a verifier would have to reimplement git's quoting to check it.  With it
the paths are raw bytes and the split is unambiguous.

What this module does not do
----------------------------
Step 1 of issue #3279 only.  The probe is a pure primitive: it is not wired
into scheduling, it emits no chain entry, and nothing in the tree calls it
yet.  Probe scheduling (step 2), the chain-anchored receipt (step 3), the
deterministic divergence response (step 4) and offline re-derivation in
``bernstein audit verify`` (step 5) are separate changes.

Degraded modes, stated rather than papered over
-----------------------------------------------
* **Textual only.**  A clean probe proves the two trees compose *textually*.
  It says nothing about a signature change in one worktree breaking a call
  site in the other.  The verdict is therefore spelled
  :data:`ProbeVerdict.TEXTUAL_CLEAN` and never ``SAFE`` -- no such member
  exists, so no caller can spell a claim stronger than what was measured.
* **Committed state only.**  Uncommitted work in a worktree is invisible to
  the probe.  A caller that records the last probed commit per task makes the
  unprobed window between it and merge-back explicit rather than assumed
  empty.
* **git < 2.38.**  ``--write-tree`` does not exist there, so probing is off
  and the verdict is :data:`ProbeVerdict.UNAVAILABLE`.  There is deliberately
  no fallback to the old positional ``git merge-tree <base> <ours> <theirs>``
  form: its output is a diff-like text stream rather than a stable artefact,
  and an unverifiable fallback would be worse than no probe.
* **Merge configuration drift.**  ``.gitattributes`` merge drivers and the
  rename-detection knobs change what a merge produces, so
  :func:`merge_config_digest` binds them into the result.  A recorded probe
  stays re-derivable only under its own recorded digest; a verifier seeing a
  changed digest must report configuration drift, not tampering.

Failure is a verdict, never an exception
----------------------------------------
Unresolvable commits, unrelated histories, a missing git binary and a timeout
all return :data:`ProbeVerdict.UNAVAILABLE` carrying a ``reason``.  Nothing
here raises.  ``UNAVAILABLE`` is a distinct member precisely so a failed probe
can never be read as evidence that two worktrees compose.

Cost note
---------
The probe mutates no working tree, no index and no branch -- but
``--write-tree`` does write tree objects into the object database.  Under a
per-commit probe cadence that is loose-object growth, which is what step 2's
per-run probe budget exists to bound.

Ordering
--------
The pair is *ordered*.  ``(a, b)`` and ``(b, a)`` are distinct probes: the two
sides map to ours/theirs, so conflict output can differ between them.  Callers
must fix the order from a canonical source -- chain order, in step 2 -- rather
than from arrival order, which is a race.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from bernstein.core.git.git_basic import run_git

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

#: ``git merge-tree --write-tree`` landed in git 2.38.  Below this the probe
#: is unavailable; see the module docstring for why there is no fallback.
PROBE_MIN_GIT_VERSION = (2, 38)

#: Default seconds before a probe subprocess is killed.  Larger than
#: ``run_git``'s own default because a merge over a big tree is not instant.
DEFAULT_PROBE_TIMEOUT = 60

# Config keys that can change what a merge produces.  Bound into
# ``merge_config_digest`` so a probe is re-derivable only under the settings it
# actually ran with.
_MERGE_CONFIG_KEYS = (
    "diff.renameLimit",
    "diff.renames",
    "merge.conflictStyle",
    "merge.renameLimit",
    "merge.renames",
)

_GIT_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")
# sha1 (40) or sha256 (64) object ids; git prints them lowercase.
_OID_RE = re.compile(r"\A[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")

# ``reason`` values on an UNAVAILABLE verdict.  Stable strings: a recorded
# probe carries them, so treat these as append-only.
REASON_NONE = ""
REASON_GIT_MISSING = "git_missing"
REASON_GIT_TOO_OLD = "git_too_old"
REASON_UNRESOLVED_COMMIT = "unresolved_commit"
REASON_NO_MERGE_BASE = "no_merge_base"
REASON_GIT_FAILED = "git_failed"
REASON_MALFORMED_OUTPUT = "malformed_output"
REASON_TIMEOUT = "timeout"


class ProbeVerdict(StrEnum):
    """What an integration probe concluded.

    There is no ``SAFE`` member, and adding one would be a mistake: a clean
    probe is a statement about textual composition only (see the module
    docstring).  ``UNAVAILABLE`` is deliberately distinct from
    ``TEXTUAL_CLEAN`` so a probe that could not run is never mistaken for a
    probe that found nothing wrong.
    """

    TEXTUAL_CLEAN = "TEXTUAL_CLEAN"
    CONFLICTED = "CONFLICTED"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class MergeTreeProbe:
    """One recomputable statement about what two live worker commits compose to.

    Frozen on purpose: this is the subject a later step hash-links and signs,
    so it must not be mutable after the measurement that produced it.

    Attributes:
        a_commit: Resolved object id of the first side (ours).  The caller's
            input is resolved before recording, so the field names an
            immutable commit rather than a branch that later moves.
        b_commit: Resolved object id of the second side (theirs).
        merge_base: Resolved merge base of the pair, recorded for
            re-derivation.  Empty when the probe never got that far.
        tree_id: Object id of the merged tree.  On a conflicting probe this is
            the tree of the *conflicted* result, which is still a real object.
            Empty only when the verdict is ``UNAVAILABLE``.
        conflicted_paths: Sorted, deduplicated paths git reported as
            conflicted.  Empty on a clean or unavailable probe.
        conflicted_paths_digest: ``sha256:`` digest over the canonical JSON of
            ``conflicted_paths``.  Always computed, so a clean probe carries
            the digest of the empty list rather than a special case.
        verdict: See :class:`ProbeVerdict`.
        exit_status: Raw git exit code (0 clean, 1 conflicted), or ``-1`` when
            git never ran.
        reason: Empty unless ``verdict`` is ``UNAVAILABLE``; one of the
            ``REASON_*`` constants.
        git_version: The git that produced this result, ``(0, 0, 0)`` when it
            could not be determined.
        merge_config_digest: See :func:`merge_config_digest`.
    """

    a_commit: str
    b_commit: str
    merge_base: str
    tree_id: str
    conflicted_paths: tuple[str, ...]
    conflicted_paths_digest: str
    verdict: ProbeVerdict
    exit_status: int
    reason: str
    git_version: tuple[int, int, int]
    merge_config_digest: str

    @property
    def is_conflicted(self) -> bool:
        """True only for a probe that actually observed a conflict."""
        return self.verdict is ProbeVerdict.CONFLICTED

    @property
    def probed(self) -> bool:
        """True when git ran and produced a usable answer either way."""
        return self.verdict is not ProbeVerdict.UNAVAILABLE


# ---------------------------------------------------------------------------
# git plumbing helpers
# ---------------------------------------------------------------------------


def _safe_git(args: list[str], cwd: Path, timeout: int) -> tuple[str, str, int] | None:
    """Run git, converting every failure mode into ``None``.

    ``run_git`` propagates ``TimeoutExpired`` and ``OSError`` (a missing git
    binary).  The probe promises never to raise, so those are absorbed here and
    surface as an ``UNAVAILABLE`` verdict instead.
    """
    try:
        result = run_git(args, cwd, timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.warning("merge-tree probe: git %s timed out after %ss", args[0], timeout)
        return None
    except OSError as exc:
        logger.warning("merge-tree probe: git %s could not run: %s", args[0], exc)
        return None
    return result.stdout, result.stderr, result.returncode


def git_version(cwd: Path, *, timeout: int = 10) -> tuple[int, int, int] | None:
    """Return the local git version as ``(major, minor, patch)``.

    Returns ``None`` when git is absent or its banner cannot be parsed.  A
    missing patch component reads as ``0`` (``"git version 2.38"`` is
    ``(2, 38, 0)``); trailing vendor suffixes such as ``.windows.1`` are
    ignored.
    """
    raw = _safe_git(["--version"], cwd, timeout)
    if raw is None:
        return None
    stdout, _stderr, returncode = raw
    if returncode != 0:
        return None
    match = _GIT_VERSION_RE.search(stdout)
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3) or 0))


def supports_write_tree(version: tuple[int, int, int] | None) -> bool:
    """True when *version* understands ``git merge-tree --write-tree``."""
    if version is None:
        return False
    return version >= PROBE_MIN_GIT_VERSION


def _digest(payload: object) -> str:
    """``sha256:`` digest over canonical JSON, matching the lineage convention."""
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _digest_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def merge_config_digest(cwd: Path, *, timeout: int = 10) -> str:
    """Digest the merge configuration that can change what a merge produces.

    Covers every tracked ``.gitattributes`` by content -- merge drivers and
    ``text``/``eol`` attributes are declared there, and a driver in a
    subdirectory changes results just as much as one at the root -- plus the
    rename-detection and conflict-style knobs in :data:`_MERGE_CONFIG_KEYS`.

    An unset config key records as JSON ``null`` rather than ``""``, so "unset"
    and "set to empty" stay distinguishable.  A tracked attributes file that
    cannot be read records as ``null`` for the same reason: the digest states
    what was observed and never silently substitutes an empty file.

    Attributes are read from the working tree because that is where git reads
    them from when it merges, so the digest describes the configuration the
    probe actually ran under.
    """
    attributes: dict[str, str | None] = {}
    listed = _safe_git(["ls-files", "-z", "--", "*.gitattributes"], cwd, timeout)
    if listed is not None and listed[2] == 0:
        for rel in sorted(entry for entry in listed[0].split("\0") if entry):
            try:
                attributes[rel] = _digest_bytes((cwd / rel).read_bytes())
            except OSError:
                attributes[rel] = None

    config: dict[str, str | None] = {}
    for key in _MERGE_CONFIG_KEYS:
        got = _safe_git(["config", "--get", key], cwd, timeout)
        config[key] = got[0].strip() if got is not None and got[2] == 0 else None

    return _digest({"config": config, "gitattributes": attributes})


def _parse_probe_output(stdout: str) -> tuple[str, tuple[str, ...]] | None:
    """Split ``--name-only -z`` output into the tree id and conflicted paths.

    The output is ``<tree-oid> NUL (<path> NUL)* NUL <informational messages>``.
    Everything from the empty terminator onward is git's human-readable
    narration ("Auto-merging f.txt", "CONFLICT (content): ..."), which is prose
    rather than a stable artefact and is deliberately dropped: it is not parsed
    and it must not reach any digest.

    Returns ``None`` when the leading field is not a plausible object id.
    """
    fields = stdout.split("\0")
    tree_id = fields[0].strip()
    if not _OID_RE.match(tree_id):
        return None

    paths: list[str] = []
    for field in fields[1:]:
        if not field:
            break
        paths.append(field)

    return tree_id, tuple(sorted(set(paths)))


# ---------------------------------------------------------------------------
# Public probe API
# ---------------------------------------------------------------------------


def _unavailable(
    *,
    a_commit: str,
    b_commit: str,
    merge_base: str,
    reason: str,
    version: tuple[int, int, int],
    config_digest: str,
    exit_status: int = -1,
) -> MergeTreeProbe:
    return MergeTreeProbe(
        a_commit=a_commit,
        b_commit=b_commit,
        merge_base=merge_base,
        tree_id="",
        conflicted_paths=(),
        conflicted_paths_digest=_digest([]),
        verdict=ProbeVerdict.UNAVAILABLE,
        exit_status=exit_status,
        reason=reason,
        git_version=version,
        merge_config_digest=config_digest,
    )


def probe_integration(
    a_commit: str,
    b_commit: str,
    cwd: Path,
    *,
    version: tuple[int, int, int] | None = None,
    timeout: int = DEFAULT_PROBE_TIMEOUT,
) -> MergeTreeProbe:
    """Probe what two live worker commits currently compose to.

    Runs ``git merge-tree --write-tree --name-only -z <a> <b>``.  No working
    tree, index or branch is touched; the merged tree object is written to the
    object database (see the module docstring's cost note) and named on stdout.

    The pair is ordered: ``a_commit`` is ours, ``b_commit`` is theirs, and
    swapping them is a different probe.

    Args:
        a_commit: First side.  Any rev git accepts; it is resolved to an object
            id before being recorded.
        b_commit: Second side, resolved the same way.
        cwd: Repository root.
        version: Pre-resolved git version.  Pass it when probing repeatedly to
            avoid respawning ``git --version`` per probe; omit to detect once
            here.
        timeout: Seconds before any single git subprocess is killed.

    Returns:
        A :class:`MergeTreeProbe`.  Never raises: every failure mode arrives as
        ``ProbeVerdict.UNAVAILABLE`` with a ``reason``.
    """
    resolved_version = version if version is not None else git_version(cwd, timeout=timeout)
    if resolved_version is None:
        # git could not be run at all, so there is no configuration to digest.
        return _unavailable(
            a_commit=a_commit,
            b_commit=b_commit,
            merge_base="",
            reason=REASON_GIT_MISSING,
            version=(0, 0, 0),
            config_digest=_digest({"config": {}, "gitattributes": {}}),
        )

    config_digest = merge_config_digest(cwd, timeout=timeout)

    if not supports_write_tree(resolved_version):
        logger.debug(
            "merge-tree probe: git %s predates %s, probing is off",
            ".".join(str(part) for part in resolved_version),
            ".".join(str(part) for part in PROBE_MIN_GIT_VERSION),
        )
        return _unavailable(
            a_commit=a_commit,
            b_commit=b_commit,
            merge_base="",
            reason=REASON_GIT_TOO_OLD,
            version=resolved_version,
            config_digest=config_digest,
        )

    resolved: list[str] = []
    for rev in (a_commit, b_commit):
        got = _safe_git(["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"], cwd, timeout)
        if got is None or got[2] != 0 or not got[0].strip():
            return _unavailable(
                a_commit=a_commit,
                b_commit=b_commit,
                merge_base="",
                reason=REASON_UNRESOLVED_COMMIT,
                version=resolved_version,
                config_digest=config_digest,
            )
        resolved.append(got[0].strip())
    a_sha, b_sha = resolved

    base = _safe_git(["merge-base", a_sha, b_sha], cwd, timeout)
    if base is None or base[2] != 0 or not base[0].strip():
        # Unrelated histories have nothing to compose against.  That is a
        # distinct fact from "these compose cleanly", hence UNAVAILABLE.
        return _unavailable(
            a_commit=a_sha,
            b_commit=b_sha,
            merge_base="",
            reason=REASON_NO_MERGE_BASE,
            version=resolved_version,
            config_digest=config_digest,
        )
    merge_base = base[0].strip()

    probe = _safe_git(["merge-tree", "--write-tree", "--name-only", "-z", a_sha, b_sha], cwd, timeout)
    if probe is None:
        return _unavailable(
            a_commit=a_sha,
            b_commit=b_sha,
            merge_base=merge_base,
            reason=REASON_TIMEOUT,
            version=resolved_version,
            config_digest=config_digest,
        )
    stdout, stderr, returncode = probe

    # 0 = clean, 1 = conflicted; anything else is git failing, not a verdict.
    if returncode not in (0, 1):
        logger.warning(
            "merge-tree probe: git exited %d for %s..%s: %s",
            returncode,
            a_sha,
            b_sha,
            stderr.strip(),
        )
        return _unavailable(
            a_commit=a_sha,
            b_commit=b_sha,
            merge_base=merge_base,
            reason=REASON_GIT_FAILED,
            version=resolved_version,
            config_digest=config_digest,
            exit_status=returncode,
        )

    parsed = _parse_probe_output(stdout)
    if parsed is None:
        logger.warning("merge-tree probe: unparsable output for %s..%s", a_sha, b_sha)
        return _unavailable(
            a_commit=a_sha,
            b_commit=b_sha,
            merge_base=merge_base,
            reason=REASON_MALFORMED_OUTPUT,
            version=resolved_version,
            config_digest=config_digest,
            exit_status=returncode,
        )
    tree_id, conflicted_paths = parsed

    verdict = ProbeVerdict.CONFLICTED if returncode == 1 else ProbeVerdict.TEXTUAL_CLEAN
    return MergeTreeProbe(
        a_commit=a_sha,
        b_commit=b_sha,
        merge_base=merge_base,
        tree_id=tree_id,
        conflicted_paths=conflicted_paths,
        conflicted_paths_digest=_digest(list(conflicted_paths)),
        verdict=verdict,
        exit_status=returncode,
        reason=REASON_NONE,
        git_version=resolved_version,
        merge_config_digest=config_digest,
    )
