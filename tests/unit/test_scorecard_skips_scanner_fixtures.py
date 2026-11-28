"""OSSF Scorecard must not report vulnerabilities from scanner test fixtures (#6068).

Each scanner adapter under ``tests/fixtures/scanners/<tool>/`` keeps a
``target/`` directory of deliberately vulnerable inputs (for example,
``osv-scanner/target/Cargo.lock`` pinning ``smallvec 1.6.0`` and
``trivy/target/package-lock.json`` pinning ``lodash 4.17.20``).

OSSF Scorecard's ``Vulnerabilities`` check runs ``osv-scanner`` recursively
from the repository root with ``UseGitignore=true`` and does not expose a
workflow ``skip-dirs`` input (and ``scorecard.yml``'s ``analysis`` job must
stay strictly ``uses``-only for ``publish_results: true``). Scoping
``tests/fixtures/scanners/.gitignore`` to ignore ``*/target/`` makes
repo-root scans skip the deliberate fixture trees while keeping the files
tracked in git and leaving ``<tool>/target/`` clean (no ``osv-scanner.toml``)
for direct adapter runs.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SCORECARD_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "scorecard.yml"
SCANNER_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "scanners"

_LOCKFILE_NAMES = frozenset(
    {
        "Cargo.lock",
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "poetry.lock",
        "uv.lock",
        "Pipfile.lock",
        "go.sum",
        "Gemfile.lock",
        "composer.lock",
    }
)


def _git_check_ignore(rel_paths: list[str]) -> set[str]:
    """Return the subset of repo-relative paths matched by gitignore rules."""
    proc = subprocess.run(
        ["git", "check-ignore", "--no-index", "--", *rel_paths],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode in (0, 1), proc.stderr
    return {line.strip().replace("\\", "/") for line in proc.stdout.splitlines() if line.strip()}


def _fixture_targets() -> list[Path]:
    targets = sorted(path for path in SCANNER_FIXTURES.glob("*/target") if path.is_dir())
    assert targets, "expected scanner fixture target trees under tests/fixtures/scanners"
    return targets


def _fixture_lockfiles() -> list[str]:
    found = sorted(
        path.relative_to(REPO_ROOT).as_posix()
        for target in _fixture_targets()
        for path in target.rglob("*")
        if path.is_file() and path.name in _LOCKFILE_NAMES
    )
    assert found, "expected deliberately vulnerable lockfiles under tests/fixtures/scanners/*/target"
    return found


def test_every_scanner_fixture_target_is_gitignored_for_scorecard() -> None:
    """Every scanner fixture target and lockfile is matched by gitignore so Scorecard skips it."""
    target_rels = [target.relative_to(REPO_ROOT).as_posix() for target in _fixture_targets()]
    lockfile_rels = _fixture_lockfiles()

    ignored_targets = _git_check_ignore(target_rels)
    ignored_lockfiles = _git_check_ignore(lockfile_rels)

    missing_targets = sorted(set(target_rels) - ignored_targets)
    missing_lockfiles = sorted(set(lockfile_rels) - ignored_lockfiles)
    assert not missing_targets, f"Scorecard can still walk fixture targets: {missing_targets}"
    assert not missing_lockfiles, f"Scorecard can still scan fixture lockfiles: {missing_lockfiles}"


def test_shipped_repository_lockfiles_remain_scanned_by_scorecard() -> None:
    """Fixture gitignore rules must never hide real repository dependency lockfiles."""
    shipped = [
        rel
        for rel in ("uv.lock", "packages/vscode/package-lock.json", "web/package-lock.json")
        if (REPO_ROOT / rel).is_file()
    ]
    assert shipped, "expected at least one shipped repository lockfile"

    ignored = _git_check_ignore(shipped)
    assert not ignored, f"shipped lockfiles were accidentally gitignored: {sorted(ignored)}"


def test_scanner_fixture_lockfiles_stay_tracked_and_target_dirs_have_no_osv_config() -> None:
    """Fixture lockfiles stay tracked in git and target trees stay free of osv-scanner.toml."""
    lockfile_rels = _fixture_lockfiles()
    proc = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", *lockfile_rels],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"fixture lockfiles must stay git-tracked: {proc.stderr}"

    configs = [
        (target / "osv-scanner.toml").relative_to(REPO_ROOT).as_posix()
        for target in _fixture_targets()
        if (target / "osv-scanner.toml").exists()
    ]
    assert not configs, f"do not place osv-scanner.toml inside fixture targets: {configs}"


def test_scorecard_analysis_job_stays_pure_uses_with_publish_results() -> None:
    """Scorecard's analysis job must remain uses-only so publish_results keeps working."""
    workflow: dict[str, Any] = yaml.safe_load(SCORECARD_WORKFLOW.read_text(encoding="utf-8"))
    steps: list[dict[str, Any]] = workflow["jobs"]["analysis"]["steps"]
    run_steps = [step.get("name", "<unnamed>") for step in steps if "run" in step]
    assert not run_steps, f"scorecard analysis job must be uses-only, found run steps: {run_steps}"

    scorecard_steps = [step for step in steps if "ossf/scorecard-action" in str(step.get("uses", ""))]
    assert len(scorecard_steps) == 1
    assert scorecard_steps[0].get("with", {}).get("publish_results") is True
