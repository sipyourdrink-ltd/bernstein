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
            reverted = _reverted_commits(history)
            records = re.findall(r"(?ms)([0-9a-f]{40})\x00(.*?)(?=\n?[0-9a-f]{40}\x00|\Z)", history)
            for sha, _message in records:
                if sha in reverted or _message.lstrip().startswith("Revert "):
                    continue
                changed = _git(
                    repo_root, "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", sha
                ).splitlines()
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
    return subprocess.check_output(("git", "-C", str(repo_root), *args), text=True)


def _reverted_commits(history: str) -> set[str]:
    """Return commits explicitly reverted in a ``git log --format=%B`` result."""
    return set(re.findall(r"This reverts commit ([0-9a-f]{40})", history))
