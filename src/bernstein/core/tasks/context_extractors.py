import subprocess
from collections import Counter
from pathlib import Path

from bernstein.core.quality.flaky_detector import FlakyDetector


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
    """Map source targets to tests co-changed by commits explicitly marked green.

    The marker is deliberately conservative: only commit subjects containing
    ``green`` count as landed-green evidence.  This makes the result
    deterministic and avoids treating an unverified local commit as evidence.
    """
    result: dict[str, list[str]] = {}
    for target in sorted(set(targets)):
        counts: Counter[str] = Counter()
        try:
            commits = _git(repo_root, "log", "--format=%H%x00%s", "--", target).splitlines()
            for record in commits:
                sha, subject = record.split("\x00", 1)
                if "green" not in subject.casefold():
                    continue
                changed = _git(
                    repo_root, "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", sha
                ).splitlines()
                for path in changed:
                    if path.startswith(("test/", "tests/")) and path.endswith(".py"):
                        counts[path] += 1
        except (OSError, subprocess.CalledProcessError, ValueError):
            result[target] = []
            continue
        result[target] = sorted(counts, key=lambda path: (-counts[path], path))[:limit]
    return result


def _git(repo_root: Path, *args: str) -> str:
    return subprocess.check_output(("git", "-C", str(repo_root), *args), text=True)
