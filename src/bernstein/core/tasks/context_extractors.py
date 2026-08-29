import logging
import re
import subprocess
from collections import Counter
from pathlib import Path

from bernstein.core.quality.flaky_detector import FlakyDetector

logger = logging.getLogger(__name__)


def find_nearest_agents_md(target_path: Path, repo_root: Path) -> str | None:
    """Finds the closest AGENTS.md by walking up the tree from target_path."""
    current = target_path.resolve()
    root = repo_root.resolve()

    # Fail-open: if the path is outside the repo for some reason, return None
    if not current.is_relative_to(root):
        return None

    while current.is_relative_to(root):
        agents_file = current / "AGENTS.md"
        if agents_file.is_file():
            # Verbatim read: no truncation or summarisation
            return agents_file.read_text(encoding="utf-8")
        if current == root:
            break
        current = current.parent

    return None


def get_known_flaky_tests(workdir: Path) -> list[str]:
    """Return the test ids the flaky detector currently has quarantined.

    Flakiness is not re-derived here. ``FlakyDetector`` owns the per-test
    history in ``.sdd/metrics/test_runs.jsonl`` and the score that promotes a
    test into ``.sdd/runtime/flaky_quarantine.json``, and the gate runner
    already deselects against that same file. A second implementation
    scoring the same evidence under its own rules would put one answer in
    the agent's prompt while the gate acted on another.

    Sorted, because the pack this feeds is content-addressed: two assemblies
    over the same quarantine must produce the same bytes.
    """
    return sorted(FlakyDetector(workdir).get_quarantined())


def extract_co_change_neighbours(repo_root: Path, targets: list[str], *, limit: int = 20) -> dict[str, list[str]]:
    """Return files that co-change with each target in the repository history.

    Frequency is the primary ranking signal. When files have the same
    frequency, the file seen in the more recent target commit wins, followed
    by a path tie-breaker. The commit graph is the source of truth, so files
    in unrelated directories are included and same-directory files are not
    preferred implicitly. History failures fail open with an empty result.
    """
    result: dict[str, list[str]] = {}
    for target in sorted(set(targets)):
        counts: Counter[str] = Counter()
        latest: dict[str, int] = {}
        try:
            commits = _git(repo_root, "log", "--format=%H", "--", target).splitlines()
            for position, commit in enumerate(commits):
                changed = _git(
                    repo_root,
                    "diff-tree",
                    "--root",
                    "--no-commit-id",
                    "--name-only",
                    "-r",
                    "-z",
                    commit,
                ).split("\0")
                for path in changed:
                    if path and path != target:
                        counts[path] += 1
                        latest[path] = max(latest.get(path, 0), len(commits) - position)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            logger.warning("could not derive co-change neighbours for %s: %s", target, exc)
            result[target] = []
            continue

        ranked = sorted(counts, key=lambda path: (-counts[path], -latest[path], path))
        if len(ranked) > limit:
            logger.info(
                "co-change neighbours truncated for %s: kept %d of %d",
                target,
                limit,
                len(ranked),
            )
        result[target] = ranked[:limit]
    return result


def extract_test_to_source_map(repo_root: Path, targets: list[str], *, limit: int = 20) -> dict[str, list[str]]:
    """Map source targets to tests co-changed by unreverted commits.

    The commit graph is the available landed-green evidence: commits reachable
    from the checked-out history are candidates, while an explicit Git revert
    removes the reverted commit from the map.  This is deterministic and does
    not infer CI status from commit-message wording.
    """
    result: dict[str, list[str]] = {}
    for target in sorted(set(targets)):
        counts: Counter[str] = Counter()
        try:
            history = _git(repo_root, "log", "--format=%H%x00%B", "--", target)
            records = re.findall(r"(?ms)([0-9a-f]{40})\x00(.*?)(?=\n?[0-9a-f]{40}\x00|\Z)", history)
            excluded = _revert_pairs(records)
            for sha, _message in records:
                if sha in excluded:
                    continue
                changed = _git(
                    repo_root, "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", "-z", sha
                ).split("\0")
                for path in changed:
                    if path.startswith(("test/", "tests/")) and path.endswith(".py"):
                        counts[path] += 1
        except (OSError, subprocess.CalledProcessError, ValueError) as exc:
            logger.warning("could not derive test-to-source history for %s: %s", target, exc)
            result[target] = []
            continue
        if len(counts) > limit:
            logger.info("test-to-source map truncated for %s: kept %d of %d tests", target, limit, len(counts))
        result[target] = sorted(counts, key=lambda path: (-counts[path], path))[:limit]
    return result


def _git(repo_root: Path, *args: str) -> str:
    return subprocess.check_output(("git", "-C", str(repo_root), *args), text=True, encoding="utf-8")


def _revert_pairs(records: list[tuple[str, str]]) -> set[str]:
    """Return the shas of every revert commit and the commit each one undid.

    The evidence is the ``This reverts commit <sha>`` trailer that ``git
    revert`` writes into the commit it creates -- a property of the record,
    not of how someone worded a subject line. Matching on a ``Revert `` prefix
    instead would drop an ordinary commit called "Revert to the previous retry
    policy" and keep a revert whose message was rewritten by hand.

    A revert that does not itself touch the target never appears in that
    target's log, so the commit it undid still counts here. Narrowing that
    needs the full history rather than the per-target one.
    """
    excluded: set[str] = set()
    for sha, message in records:
        undone = re.search(r"This reverts commit ([0-9a-f]{40})", message)
        if undone is not None:
            excluded.add(sha)
            excluded.add(undone.group(1))
    return excluded
