"""Two-way ratchet assertion helper for snapshot allowlists (#5552, #5503).

Guards that hold a frozen snapshot (``KNOWN_ORPHANS``, ``KNOWN_UNCALLED``,
``KNOWN_UNREACHABLE``) must report both directions of drift in a single run:
- entries missing from the snapshot (new caller-less code)
- entries stale in the snapshot (code that gained a caller or was removed)

When git metadata is available, it distinguishes between entries introduced
by the branch versus entries added on ``main`` while the branch was in flight,
explaining whether the branch is at fault or the baseline snapshot is stale.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def _branch_changed_files() -> set[str] | None:
    """Return repo-relative files modified on this branch against its merge base."""
    try:
        # Check if inside a git repository
        is_git = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if is_git.returncode != 0 or is_git.stdout.strip() != "true":
            return None

        # On GitHub Actions PR merge builds, HEAD is refs/pull/<n>/merge with 2 parents:
        # HEAD^1 is target branch (main), HEAD^2 is the PR branch.
        parents = subprocess.run(
            ["git", "rev-parse", "--parents", "-n", "1", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if parents.returncode == 0:
            parts = parents.stdout.strip().split()
            if len(parts) >= 3:
                # Merge commit: diff first parent against second parent (the PR branch diff)
                diff = subprocess.run(
                    ["git", "diff", "--name-only", parts[1], parts[2]],
                    cwd=REPO_ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if diff.returncode == 0:
                    return {p.strip().replace("\\", "/") for p in diff.stdout.splitlines() if p.strip()}

        # Otherwise try merge-base against origin/HEAD, origin/main, or upstream/main
        base = None
        for candidate in ("origin/HEAD", "origin/main", "upstream/main", "main"):
            mb = subprocess.run(
                ["git", "merge-base", candidate, "HEAD"],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            if mb.returncode == 0 and mb.stdout.strip():
                base = mb.stdout.strip()
                break

        if base:
            diff = subprocess.run(
                ["git", "diff", "--name-only", base, "HEAD"],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            if diff.returncode == 0:
                status = subprocess.run(
                    ["git", "status", "--porcelain"],
                    cwd=REPO_ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                changed = {p.strip().replace("\\", "/") for p in diff.stdout.splitlines() if p.strip()}
                if status.returncode == 0:
                    for line in status.stdout.splitlines():
                        if len(line) > 3:
                            changed.add(line[3:].strip().replace("\\", "/"))
                return changed

        # Fallback to plain working tree diff against HEAD
        diff = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if diff.returncode == 0:
            return {p.strip().replace("\\", "/") for p in diff.stdout.splitlines() if p.strip()}

    except (OSError, ValueError):
        pass

    return None


def format_snapshot_snippet(constant_name: str, entries: Iterable[str]) -> str:
    """Format a copy-pasteable Python snippet for updating the snapshot constant."""
    sorted_entries = sorted(entries)
    if not sorted_entries:
        return f"{constant_name} = frozenset()"
    lines = [f"{constant_name} = frozenset({{"]
    for entry in sorted_entries:
        lines.append(f'    "{entry}",')
    lines.append("})")
    return "\n".join(lines)


def assert_ratchet_matches(
    current: Iterable[str],
    baseline: Iterable[str],
    *,
    subject: str,
    constant_name: str = "KNOWN_ORPHANS",
    file_mapping: dict[str, str | Path] | None = None,
    wire_hint: str | None = None,
) -> None:
    """Assert that current matches baseline, reporting all two-way drift in one error.

    Args:
        current: The currently computed set of items from the source tree.
        baseline: The frozen snapshot / allowlist set.
        subject: Human-readable subject description (e.g. 'core/tokens/ orphan allowlist').
        constant_name: The Python constant name in the test module (e.g. 'KNOWN_ORPHANS').
        file_mapping: Optional mapping from entry string to repo-relative file path,
            used to attribute whether a new entry was touched on this branch.
        wire_hint: Actionable advice for wiring newly appeared entries.
    """
    current_set = set(current)
    baseline_set = set(baseline)

    appeared = sorted(current_set - baseline_set)
    removed = sorted(baseline_set - current_set)

    if not appeared and not removed:
        return

    # Check git attribution if available
    branch_files = _branch_changed_files()
    branch_appeared: list[str] = []
    stale_from_main: list[str] = []

    if branch_files is not None and file_mapping is not None:
        for entry in appeared:
            rel_path = file_mapping.get(entry)
            if rel_path is not None:
                norm_path = str(rel_path).replace("\\", "/").strip("/")
                if any(norm_path == f or norm_path.startswith(f) or f.startswith(norm_path) for f in branch_files):
                    branch_appeared.append(entry)
                else:
                    stale_from_main.append(entry)
            else:
                branch_appeared.append(entry)
    else:
        branch_appeared = appeared

    sections: list[str] = [f"Drift detected in {subject}:"]

    if branch_appeared:
        sections.append(f"\n  [Branch Changes] New entries introduced by this branch ({len(branch_appeared)}):")
        for item in branch_appeared:
            sections.append(f"    + {item}")
        if wire_hint:
            sections.append(f"    --> {wire_hint}")

    if stale_from_main:
        sections.append(
            f"\n  [Baseline Stale] Entries present on main but missing in snapshot ({len(stale_from_main)}):"
        )
        for item in stale_from_main:
            sections.append(f"    + {item} (from main: file not touched by this branch)")
        sections.append("    --> The branch is not at fault. Rebase onto main to update the snapshot.")

    if removed:
        sections.append(
            f"\n  [Stale Exemptions] Entries in {constant_name} that no longer exist or now have callers ({len(removed)}):"
        )
        for item in removed:
            sections.append(f"    - {item}")
        sections.append(f"    --> Remove these from {constant_name} so the allowlist keeps shrinking.")

    sections.append(f"\nTo update {constant_name} to match the current tree, use:\n")
    sections.append(format_snapshot_snippet(constant_name, current_set))

    raise AssertionError("\n".join(sections))
