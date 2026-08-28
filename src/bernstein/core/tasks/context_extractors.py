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
