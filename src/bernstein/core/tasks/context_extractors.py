import json
import logging
from pathlib import Path

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


def get_known_flaky_tests(journal_paths: list[Path]) -> list[str]:
    """
    Parses run journals to find tests that failed and passed on the same commit.
    We process a 14-day window of journals (enforced by the caller providing the paths).
    """
    test_states: dict[str, tuple[str, str]] = {}
    flaky_tests: set[str] = set()

    for path in journal_paths:
        try:
            content = path.read_text(encoding="utf-8")
            runs = json.loads(content)

            for run in runs:
                test_id = run.get("test_id")
                status = run.get("status")
                commit = run.get("commit")

                if not all([test_id, status, commit]):
                    continue

                if test_id in test_states:
                    prev_status, prev_commit = test_states[test_id]
                    # Flaky: failed and then passed without a source change (same commit)
                    if prev_status == "failed" and status == "passed" and prev_commit == commit:
                        flaky_tests.add(test_id)

                test_states[test_id] = (status, commit)

        except Exception as e:
            logger.warning(f"Failed to read run journal {path}: {e}")

    return sorted(list(flaky_tests))
