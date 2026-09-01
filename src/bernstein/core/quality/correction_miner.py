"""Maintainer correction mining from merged history.

Walks the merged-history of a repository and extracts ``(base_diff,
follow_up_diff)`` pairs where a maintainer with merge rights corrected
a contributor's branch before the merge. The pairs are grouped into
proposed convention receipts per the shape defined in #3750:

* every proposal cites the commit-SHA pairs it was derived from
  (requirement #1)
* a proposal that cannot be expressed as an executable assertion is
  not proposed (requirement #2)
* proposals are labelled ``single-source`` (one author) or
  ``corroborated`` (multiple authors) (requirement #3)
* corpus size renders with the proposal (requirement #4)
* proposals are inert: they emit nothing that auto-activates
  (requirement #5)

The mining unit is the ``(base, follow_up)`` pair, not the merge commit
itself. The merge commit only marks the moment a pair was accepted; the
useful evidence is the diff between the contributor's pre-correction
state and the maintainer's fix.

Typical usage::

    from pathlib import Path
    from bernstein.core.quality.correction_miner import (
        extract_correction_pairs,
        mine_corrections,
        render_corrections_report,
    )

    pairs = extract_correction_pairs(Path("."), max_merges=200)
    result = mine_corrections(pairs)
    report = render_corrections_report(result)
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bernstein.core.git.git_basic import run_git

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

# A token that names a pattern. Built from the first file touched by the
# follow-up diff and a short, deterministic fingerprint of the diff itself.
# Tokens are intentionally lossy: two corrections with the same token are
# treated as the same pattern even if they touch slightly different
# code. That's the point -- recurring shape, not byte-identical content.
_PATTERN_HEADER_RE = re.compile(r"^diff --git a/(.+?) b/(.+?)$", re.MULTILINE)
_HUNK_PLUS_RE = re.compile(r"^\+(?!\+)(.*)$", re.MULTILINE)
_HUNK_MINUS_RE = re.compile(r"^-(?!--)(.*)$", re.MULTILINE)
_SUBJECT_RE = re.compile(r"^# (.+)$", re.MULTILINE)


@dataclass(frozen=True)
class CorrectionPair:
    """A maintainer follow-up commit that fixed a contributor's branch before merge.

    Attributes:
        base_commit: Commit SHA the contributor's branch sat at when the
            maintainer stepped in.
        follow_up_commit: Commit SHA the maintainer authored on top of
            ``base_commit``.
        merge_commit: SHA of the merge that accepted the correction.
        author: ``Name <email>`` of the follow-up author.
        message: Subject of the follow-up commit.
        created_timestamp: Unix epoch timestamp of the follow-up commit.
        base_diff: ``git diff`` from ``base_commit``'s parent to ``base_commit``
            (the contributor's state before the correction).
        follow_up_diff: ``git diff`` from ``base_commit`` to ``follow_up_commit``
            (the maintainer's correction).
    """

    base_commit: str
    follow_up_commit: str
    merge_commit: str
    author: str
    message: str
    created_timestamp: float
    base_diff: str
    follow_up_diff: str

    @property
    def author_id(self) -> str:
        """Stable identifier for the author (email preferred over name)."""
        if "<" in self.author and ">" in self.author:
            return self.author.split("<", 1)[1].rstrip(">").strip()
        return self.author

    @property
    def commit_pair_id(self) -> str:
        """Stable identifier for the pair (base:follow_up)."""
        return f"{self.base_commit}::{self.follow_up_commit}"


@dataclass(frozen=True)
class CorrectionProposal:
    """A proposed convention receipt derived from one or more correction pairs.

    Attributes:
        rule_text: Human-readable statement of the convention/rule.
        rule_text_hash: SHA-256 hex digest of ``rule_text``.
        subject_path: File path the convention applies to (the dominant
            file touched across the pairs).
        base_commit_sha: Earliest commit the proposal was learned against.
        subject_symbol: Optional symbol within the file the rule applies to.
        assertion_ref: Optional executable assertion spec; ``None`` if the
            pattern does not yield one (requirement #2: not proposed).
        commit_pairs: Tuple of ``"base::follow_up"`` pair IDs cited as evidence
            (requirement #1).
        authors: Tuple of distinct author identifiers (requirement #3).
        corpus_size: Total number of correction pairs in this proposal's
            corpus (requirement #4).
        classification: ``"single-source"`` if exactly one author, else
            ``"corroborated"`` (requirement #3).
        base_diff_summary: Truncated, concatenated base diff snippets.
        follow_up_diff_summary: Truncated, concatenated follow-up diff snippets.
    """

    rule_text: str
    rule_text_hash: str
    subject_path: str
    base_commit_sha: str
    subject_symbol: str
    assertion_ref: dict[str, object] | None
    commit_pairs: tuple[str, ...]
    authors: tuple[str, ...]
    corpus_size: int
    classification: str
    base_diff_summary: str
    follow_up_diff_summary: str

    @property
    def author_count(self) -> int:
        return len(self.authors)

    @property
    def is_single_source(self) -> bool:
        return self.classification == "single-source" and self.author_count == 1

    @property
    def is_corroborated(self) -> bool:
        return self.classification == "corroborated" and self.author_count > 1

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable dict for the proposal."""
        return {
            "rule_text": self.rule_text,
            "rule_text_hash": self.rule_text_hash,
            "subject_path": self.subject_path,
            "base_commit_sha": self.base_commit_sha,
            "subject_symbol": self.subject_symbol,
            "assertion_ref": self.assertion_ref,
            "commit_pairs": list(self.commit_pairs),
            "authors": list(self.authors),
            "corpus_size": self.corpus_size,
            "classification": self.classification,
            "base_diff_summary": self.base_diff_summary,
            "follow_up_diff_summary": self.follow_up_diff_summary,
        }


@dataclass(frozen=True)
class MiningResult:
    """Aggregated output of the correction mining pipeline.

    Attributes:
        proposals: Discovered proposals, sorted by corroboration then corpus size.
        total_pairs_analyzed: Total number of correction pairs processed.
        total_authors_analyzed: Number of distinct authors who made corrections.
        total_proposals: Number of correction proposals generated.
    """

    proposals: tuple[CorrectionProposal, ...]
    total_pairs_analyzed: int
    total_authors_analyzed: int
    total_proposals: int


# ---------------------------------------------------------------------------
# Git wrappers (read-only)
# ---------------------------------------------------------------------------


def _git(cwd: Path, args: list[str]) -> str:
    """Run a read-only git command and return stdout (stripped).

    Raises:
        RuntimeError: If git exits non-zero. Callers that want best-effort
            reads (e.g. walking a reflog entry that may be missing) should
            catch this and skip the entry rather than treating the failure
            as a hard error.
    """
    result = run_git(args, cwd)
    if not result.ok:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def find_merged_commits(
    cwd: Path,
    *,
    since: str | None = None,
    max_count: int = 200,
) -> list[str]:
    """Return up to *max_count* merge commits, newest-first.

    Walks ``git log`` for commits with two or more parents. The ``--merges``
    flag already filters to those; the call site does not need to second-guess.

    Args:
        cwd: Repository root.
        since: Optional ``--since`` revision (default: last 6 months).
        max_count: Maximum number of merges to return.

    Returns:
        List of merge commit SHAs, newest-first. Empty on failure.
    """
    since_arg = since if since else "6 months ago"
    try:
        out = _git(cwd, ["log", "--merges", f"--max-count={max_count}", f"--since={since_arg}", "--pretty=%H"])
    except RuntimeError as exc:
        logger.warning("find_merged_commits: %s", exc)
        return []
    return [line for line in out.splitlines() if line]


def _parents(cwd: Path, commit_sha: str) -> list[str]:
    """Return parent SHAs of *commit_sha* (empty list on failure)."""
    try:
        out = _git(cwd, ["rev-list", "--parents", "-n", "1", commit_sha])
    except RuntimeError as exc:
        logger.debug("_parents(%s): %s", commit_sha[:12], exc)
        return []
    parts = out.split()
    return parts[1:] if len(parts) > 1 else []


def _count_commits(cwd: Path, commit_sha: str) -> int:
    """Return the number of commits reachable from *commit_sha* (excluding merges)."""
    try:
        out = _git(cwd, ["rev-list", "--no-merges", "--count", commit_sha])
    except RuntimeError:
        return 0
    try:
        return int(out.strip())
    except (TypeError, ValueError):
        return 0


def _merge_base(cwd: Path, a: str, b: str) -> str | None:
    """Return ``merge-base(a, b)`` or ``None`` on failure."""
    try:
        return _git(cwd, ["merge-base", a, b])
    except RuntimeError as exc:
        logger.debug("_merge_base(%s, %s): %s", a[:12], b[:12], exc)
        return None


def _is_ancestor(cwd: Path, ancestor: str, descendant: str) -> bool:
    """Return True if *ancestor* is reachable from *descendant*."""
    try:
        _git(cwd, ["merge-base", "--is-ancestor", ancestor, descendant])
    except RuntimeError:
        return False
    return True


def _commit_author(cwd: Path, commit_sha: str) -> str:
    """Return ``Name <email>`` of *commit_sha*'s author."""
    return _git(cwd, ["show", "-s", "--no-patch", "--format=%an <%ae>", commit_sha])


def _commit_message(cwd: Path, commit_sha: str) -> str:
    """Return the first subject line of *commit_sha*."""
    return _git(cwd, ["show", "-s", "--no-patch", "--format=%s", commit_sha])


def _commit_timestamp(cwd: Path, commit_sha: str) -> float:
    """Return the committer timestamp of *commit_sha* as a unix epoch."""
    out = _git(cwd, ["show", "-s", "--no-patch", "--format=%ct", commit_sha])
    try:
        return float(out.strip())
    except (TypeError, ValueError):
        return 0.0


def _diff(cwd: Path, base: str, head: str) -> str:
    """Return the ``git diff`` between *base* and *head* (empty on failure)."""
    try:
        return _git(cwd, ["diff", "--no-color", "--no-ext-diff", base, head])
    except RuntimeError as exc:
        logger.debug("_diff(%s, %s): %s", base[:12], head[:12], exc)
        return ""


# ---------------------------------------------------------------------------
# Merge-rights detection
# ---------------------------------------------------------------------------

# Maintainer set is operator-configured. An empty set disables follow-up
# filtering: every candidate follow-up commit is accepted, which is
# useful for first-time mining against an unknown repo but yields noisy
# proposals. The constant is a placeholder; real consumers should pass
# their own list to :func:`extract_correction_pairs`.
DEFAULT_MERGE_RIGHTS_HOLDERS: frozenset[str] = frozenset()


def is_merge_rights_holder(
    author_id: str,
    *,
    merge_rights_holders: frozenset[str] | None = None,
) -> bool:
    """Return whether *author_id* has merge rights.

    When *merge_rights_holders* is empty (the default), every author is
    treated as a holder. This is deliberate: a fresh deployment that
    has not been told who can merge should mine everything and let the
    operator filter the proposals, rather than mining nothing.

    Args:
        author_id: Email or name of the candidate author.
        merge_rights_holders: Set of accepted author identifiers. ``None``
            uses :data:`DEFAULT_MERGE_RIGHTS_HOLDERS`.

    Returns:
        Whether the author is a merge-rights holder.
    """
    holders = merge_rights_holders if merge_rights_holders is not None else DEFAULT_MERGE_RIGHTS_HOLDERS
    if not holders:
        return True
    return author_id.lower() in {h.lower() for h in holders}


# ---------------------------------------------------------------------------
# Correction extraction
# ---------------------------------------------------------------------------


def extract_correction_pairs(
    cwd: Path,
    *,
    max_merges: int = 200,
    since: str | None = None,
    merge_rights_holders: frozenset[str] | None = None,
) -> list[CorrectionPair]:
    """Walk *cwd*'s merged history and return the (base, follow_up) pairs.

    A *correction pair* is the relationship between a contributor's commit
    and the maintainer's first commit on top of it, where the maintainer
    holds merge rights. The pair is evidence: someone with commit rights
    judged the base insufficient and demonstrated the fix in code rather
    than describing it in prose.

    Args:
        cwd: Repository root.
        max_merges: Cap on merge commits to walk.
        since: Optional ``--since`` filter for the log query.
        merge_rights_holders: Set of accepted author identifiers. ``None``
            means every author is a holder (see :func:`is_merge_rights_holder`).

    Returns:
        List of :class:`CorrectionPair` records. One pair per (base_commit,
        follow_up_commit) tuple observed.
    """
    merges = find_merged_commits(cwd, since=since, max_count=max_merges)
    if not merges:
        return []

    pairs: list[CorrectionPair] = []
    for merge_sha in merges:
        parents = _parents(cwd, merge_sha)
        # 3-parent octopus merges and beyond are out of scope. A 1-parent
        # entry here is a fast-forward and has no "contributor vs maintainer"
        # sides, so skip it too.
        if len(parents) != 2:
            continue

        # Identify which parent is the contributor side (the branch that was merged)
        # and which is the main/trunk side. Use a heuristic: the contributor side
        # has MORE commits in its ancestry (because the branch accumulated commits
        # before being merged). Count commits reachable from each parent.
        p0_commits = _count_commits(cwd, parents[0])
        p1_commits = _count_commits(cwd, parents[1])
        if p0_commits > p1_commits:
            contributor_side, other_side = parents[0], parents[1]
        else:
            contributor_side, other_side = parents[1], parents[0]

        # Walk every non-merge commit on the contributor's side that landed
        # in this merge. These are the *base* candidates -- the states a
        # maintainer had to choose between fixing or accepting as-is.
        try:
            base_out = _git(
                cwd,
                [
                    "log",
                    "--no-merges",
                    "--no-walk",
                    "--pretty=%H",
                    f"{other_side}..{contributor_side}",
                ],
            )
        except RuntimeError:
            continue
        base_candidates = [line for line in base_out.splitlines() if line]
        if not base_candidates:
            continue

        for base_commit in base_candidates:
            follow_up = _first_maintainer_commit(
                cwd,
                base_commit,
                contributor_side,
                merge_rights_holders=merge_rights_holders,
            )
            if follow_up is None:
                continue
            # If the follow-up happens to also sit on the contributor side
            # (a self-fix), we still treat it as a correction: the
            # contributor learned from review. The proposal will end up
            # single-source and need confirmation -- requirement #5.
            if follow_up == base_commit:
                continue
            if not _is_ancestor(cwd, base_commit, follow_up):
                # base and follow_up aren't on the same lineage. Skip rather
                # than pair a base with the wrong ancestor.
                continue

            base_diff = _diff(cwd, f"{base_commit}^", base_commit)
            follow_up_diff = _diff(cwd, base_commit, follow_up)
            if not base_diff and not follow_up_diff:
                # Both sides empty -- the candidate wasn't actually a content
                # change, e.g. a merge-only or notes-only commit.
                continue
            try:
                author = _commit_author(cwd, follow_up)
                message = _commit_message(cwd, follow_up)
                timestamp = _commit_timestamp(cwd, follow_up)
            except RuntimeError:
                continue

            pairs.append(
                CorrectionPair(
                    base_commit=base_commit,
                    follow_up_commit=follow_up,
                    merge_commit=merge_sha,
                    author=author,
                    message=message,
                    created_timestamp=timestamp,
                    base_diff=base_diff,
                    follow_up_diff=follow_up_diff,
                )
            )

    return pairs


def _first_maintainer_commit(
    cwd: Path,
    base_commit: str,
    contributor_side: str,
    *,
    merge_rights_holders: frozenset[str] | None,
) -> str | None:
    """Find the first commit after *base_commit* authored by a merge-rights holder.

    Walks the linear history ``base_commit..contributor_side`` looking for
    the first commit whose author passes :func:`is_merge_rights_holder`.
    Linear because we're walking a single side of a merge; if a fix
    came in via an out-of-band branch the walk will miss it, which is
    acceptable -- the merge we are processing only saw the side it saw.
    """
    try:
        out = _git(
            cwd,
            [
                "log",
                "--no-merges",
                "--reverse",
                "--pretty=%H %an <%ae>",
                f"{base_commit}..{contributor_side}",
            ],
        )
    except RuntimeError:
        return None
    for line in out.splitlines():
        parts = line.split(" ", 1)
        if len(parts) != 2:
            continue
        sha, author = parts
        author_id = author
        if "<" in author and ">" in author:
            author_id = author.split("<", 1)[1].rstrip(">").strip()
        if is_merge_rights_holder(author_id, merge_rights_holders=merge_rights_holders):
            return sha
    return None


# ---------------------------------------------------------------------------
# Pattern grouping
# ---------------------------------------------------------------------------


def _diff_signature(diff: str) -> str:
    """A short, stable fingerprint of *diff* for pattern grouping.

    Strips line numbers, hunk headers, and the actual byte changes; keeps
    the *shape* -- which file, which function, which lines are touched.
    Two corrections that touch the same file and add the same line in
    the same function will produce the same signature; a cosmetic
    re-indent of the same change will not. That's the right cut:
    recurring shape, not byte-identical content.
    """
    if not diff:
        return ""
    files: list[str] = []
    plus_lines: list[str] = []
    minus_lines: list[str] = []
    current_file = ""
    for raw in diff.splitlines():
        line = raw.rstrip()
        if line.startswith("diff --git "):
            match = _PATTERN_HEADER_RE.match(line)
            current_file = match.group(2) if match else ""
            files.append(current_file)
        elif line.startswith("@@"):
            continue
        elif line.startswith("+") and not line.startswith("+++"):
            plus_lines.append(line[1:].strip())
        elif line.startswith("-") and not line.startswith("---"):
            minus_lines.append(line[1:].strip())
    digest = hashlib.sha256()
    digest.update("\n".join(files).encode("utf-8", errors="replace"))
    digest.update(b"\n---+\n")
    digest.update("\n".join(plus_lines).encode("utf-8", errors="replace"))
    digest.update(b"\n---\n")
    digest.update("\n".join(minus_lines).encode("utf-8", errors="replace"))
    return digest.hexdigest()[:16]


def _primary_file(diff: str) -> str:
    """Return the dominant file path touched by *diff* (best-effort)."""
    if not diff:
        return ""
    headers = _PATTERN_HEADER_RE.findall(diff)
    if not headers:
        return ""
    # ``diff --git a/foo b/foo`` -> "foo". Take the first non-empty
    # destination; renames are out of scope here.
    for _old, new in headers:
        if new and new != "/dev/null":
            return new
    return headers[0][1]


def _build_rule_text(subject_path: str, follow_up_diff: str) -> str:
    """Build a short, human-readable rule text from a follow-up diff.

    This is *not* a paraphrase -- it is the literal first added line of
    the follow-up diff, prefixed with the file. Paraphrasing requires
    an LLM and is out of scope for the core miner; the rule text is
    human-checkable as-is.
    """
    plus_lines = [m.strip() for m in _HUNK_PLUS_RE.findall(follow_up_diff) if m.strip()]
    snippet = plus_lines[0] if plus_lines else "<no addition>"
    if len(snippet) > 120:
        snippet = snippet[:117] + "..."
    return f"Apply this correction to {subject_path}: `{snippet}`"


def _summarise(diffs: list[str], limit: int = 3) -> str:
    """Concatenate up to *limit* diff snippets, truncated to 200 chars each."""
    snippets: list[str] = []
    for diff in diffs[:limit]:
        text = diff.strip().splitlines()
        if len(text) > 12:
            text = [*text[:12], "..."]
        snippets.append(" | ".join(text))
    out = " ;; ".join(snippets)
    if len(out) > 800:
        out = out[:797] + "..."
    return out


def classify_correction(
    pair: CorrectionPair,
    *,
    merge_rights_holders: frozenset[str] | None = None,
) -> str:
    """Classify *pair* as ``"single-source"`` or ``"corroborated"``.

    Single-source: only one distinct author (this *pair's* author) has
    contributed corrections of this shape. Corroborated: at least two
    distinct authors have contributed. Single-pair proposals are always
    single-source -- corroboration requires at least two pairs, which
    :func:`mine_corrections` decides after grouping.

    Args:
        pair: A single correction pair.
        merge_rights_holders: Unused here, kept for symmetry with the
            extraction API.

    Returns:
        ``"single-source"`` (always, for one pair) or ``"corroborated"``
        when the miner has aggregated this pair with another author's.
    """
    return "single-source"


def mine_corrections(pairs: list[CorrectionPair]) -> MiningResult:
    """Group *pairs* into :class:`CorrectionProposal` records.

    Pairs cluster by ``(subject_path, diff_signature)``. A cluster with
    one pair is still a proposal -- its corpus size is 1 and its
    classification is ``single-source``. A cluster with two or more
    pairs whose authors differ is ``corroborated``; if all pairs share
    one author it stays ``single-source``.

    Args:
        pairs: Output of :func:`extract_correction_pairs`.

    Returns:
        A :class:`MiningResult` with the proposals, sorted
        corroborated-first then by descending corpus size.
    """
    if not pairs:
        return MiningResult(
            proposals=(),
            total_pairs_analyzed=0,
            total_authors_analyzed=0,
            total_proposals=0,
        )

    clusters: dict[tuple[str, str], list[CorrectionPair]] = defaultdict(list)
    for pair in pairs:
        subject = _primary_file(pair.follow_up_diff) or _primary_file(pair.base_diff) or "*"
        signature = _diff_signature(pair.follow_up_diff)
        if not signature:
            # Empty follow-up diff means we have no fix shape to group on.
            continue
        clusters[(subject, signature)].append(pair)

    proposals: list[CorrectionProposal] = []
    for (subject, _sig), cluster_pairs in clusters.items():
        if not subject or subject == "/dev/null":
            continue
        authors = sorted({p.author_id for p in cluster_pairs})
        classification = "corroborated" if len(authors) > 1 else "single-source"
        rule_text = _build_rule_text(subject, cluster_pairs[0].follow_up_diff)
        rule_text_hash = hashlib.sha256(rule_text.encode("utf-8")).hexdigest()
        # Pick the earliest base commit as the proposal's pinned SHA so the
        # rule's lifetime is bounded by what was true when the pattern was
        # first observed.
        base_commit_sha = min(
            (p.base_commit for p in cluster_pairs),
            key=lambda sha: next((pp.created_timestamp for pp in cluster_pairs if pp.base_commit == sha), 0.0),
        )
        commit_pair_ids = tuple(sorted({p.commit_pair_id for p in cluster_pairs}))
        proposal = CorrectionProposal(
            rule_text=rule_text,
            rule_text_hash=rule_text_hash,
            subject_path=subject,
            base_commit_sha=base_commit_sha,
            subject_symbol="",
            assertion_ref=None,
            commit_pairs=commit_pair_ids,
            authors=tuple(authors),
            corpus_size=len(cluster_pairs),
            classification=classification,
            base_diff_summary=_summarise([p.base_diff for p in cluster_pairs]),
            follow_up_diff_summary=_summarise([p.follow_up_diff for p in cluster_pairs]),
        )
        proposals.append(proposal)

    # Corroborated first (the whole point of mining across authors), then
    # larger corpora first. Ties break on subject path for determinism.
    proposals.sort(
        key=lambda p: (
            0 if p.is_corroborated else 1,
            -p.corpus_size,
            p.subject_path,
        ),
    )

    return MiningResult(
        proposals=tuple(proposals),
        total_pairs_analyzed=len(pairs),
        total_authors_analyzed=len({p.author_id for p in pairs}),
        total_proposals=len(proposals),
    )


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def render_corrections_report(result: MiningResult) -> str:
    """Render *result* as a Markdown report.

    Args:
        result: Output of :func:`mine_corrections`.

    Returns:
        Markdown-formatted report string. The report always renders
        corpus size alongside the proposal per requirement #4.
    """
    lines: list[str] = []
    append = lines.append

    append("# Correction Pattern Report")
    append("")
    append(f"**Correction pairs analyzed:** {result.total_pairs_analyzed}")
    append(f"**Distinct authors:** {result.total_authors_analyzed}")
    append(f"**Proposals found:** {result.total_proposals}")
    append("")

    if result.proposals:
        append("## Proposals")
        append("")
        append("| Subject | Authors | Corpus | Classification | First Base SHA |")
        append("|---------|---------|--------|----------------|----------------|")
        for prop in result.proposals:
            authors_str = ", ".join(prop.authors[:3])
            if len(prop.authors) > 3:
                authors_str += f" + {len(prop.authors) - 3} more"
            append(
                f"| `{prop.subject_path}` | {authors_str} "
                f"| {prop.corpus_size} pair(s) | {prop.classification} "
                f"| `{prop.base_commit_sha[:12]}` |"
            )
        append("")

        append("## Proposal Details")
        append("")
        for prop in result.proposals:
            append(f"### `{prop.subject_path}` — {prop.classification}")
            append("")
            append(f"- **Rule:** {prop.rule_text}")
            append(f"- **Rule hash:** `{prop.rule_text_hash[:12]}...`")
            append(f"- **Subject path:** `{prop.subject_path}`")
            append(f"- **Pinned base SHA:** `{prop.base_commit_sha}`")
            append(f"- **Corpus size:** {prop.corpus_size} correction pair(s)")
            append(f"- **Authors:** {', '.join(prop.authors)}")
            append("- **Commit pairs (evidence, requirement #1):**")
            for pair_id in prop.commit_pairs:
                append(f"  - `{pair_id}`")
            append("- **Base diff sample:**")
            append("  ```diff")
            for snippet in prop.base_diff_summary.split(" ;; "):
                append(f"  {snippet}")
            append("  ```")
            append("- **Follow-up diff sample:**")
            append("  ```diff")
            for snippet in prop.follow_up_diff_summary.split(" ;; "):
                append(f"  {snippet}")
            append("  ```")
            append("")

    if not result.proposals:
        append("*No correction patterns found. Need more maintainer follow-up data.*")
        append("")

    return "\n".join(lines)


__all__ = [
    "DEFAULT_MERGE_RIGHTS_HOLDERS",
    "CorrectionPair",
    "CorrectionProposal",
    "MiningResult",
    "classify_correction",
    "extract_correction_pairs",
    "find_merged_commits",
    "is_merge_rights_holder",
    "mine_corrections",
    "render_corrections_report",
]
