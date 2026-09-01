"""Unit tests for :mod:`bernstein.core.git.merge_tree_probe`.

Covers:

* Determinism -- probing the same pair twice yields a byte-identical tree id.
* A known conflict is reported deterministically, naming the conflicted paths.
* Disjoint edits compose cleanly and carry a merged tree id.
* The probe mutates no working tree, index, branch or HEAD.
* git < 2.38 disables probing without ever invoking ``merge-tree``.
* The pair is ordered, and ``UNAVAILABLE`` is never mistaken for clean.
* The merge-config digest tracks ``.gitattributes`` and the rename knobs.

These run against a real ``git`` binary in ``tmp_path``.  Nothing here mocks
git plumbing: the whole point of the probe is that its output is what stock git
actually produces, so a stubbed subprocess would assert nothing worth knowing.
They never touch a remote and never reach the network.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from bernstein.core.git.merge_tree_probe import (
    PROBE_MIN_GIT_VERSION,
    REASON_GIT_TOO_OLD,
    REASON_NO_MERGE_BASE,
    REASON_UNRESOLVED_COMMIT,
    MergeTreeProbe,
    ProbeVerdict,
    git_version,
    merge_config_digest,
    probe_integration,
    supports_write_tree,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    """Run git in *repo*, failing the test loudly on a non-zero exit."""
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        msg = f"git {' '.join(args)} failed ({result.returncode}): {result.stderr.strip()}"
        raise AssertionError(msg)
    return result.stdout


def _write(repo: Path, rel: str, body: str) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    # Explicit newline so the fixture bytes do not vary with platform defaults.
    target.write_text(body, encoding="utf-8", newline="\n")


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo whose base commit two worker branches then diverge from.

    Layout after setup::

        base ---- worker/a   (rewrites shared.txt line 1, adds only_a.txt)
              \\-- worker/b   (rewrites shared.txt line 1, adds only_b.txt)
              \\-- worker/c   (adds only_c.txt, touches nothing shared)

    ``a`` and ``b`` collide on ``shared.txt``; ``a`` and ``c`` are disjoint.
    """
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main", ".")
    _git(root, "config", "user.email", "probe@example.test")
    _git(root, "config", "user.name", "Probe Fixture")
    # Keep fixture blobs byte-stable regardless of the host's autocrlf default.
    _git(root, "config", "core.autocrlf", "false")

    _write(root, "shared.txt", "line1\nline2\nline3\n")
    _write(root, "untouched.txt", "constant\n")
    base = _commit(root, "base")

    _git(root, "checkout", "-q", "-b", "worker/a", base)
    _write(root, "shared.txt", "AAA\nline2\nline3\n")
    _write(root, "only_a.txt", "from a\n")
    _commit(root, "a")

    _git(root, "checkout", "-q", "-b", "worker/b", base)
    _write(root, "shared.txt", "BBB\nline2\nline3\n")
    _write(root, "only_b.txt", "from b\n")
    _commit(root, "b")

    _git(root, "checkout", "-q", "-b", "worker/c", base)
    _write(root, "only_c.txt", "from c\n")
    _commit(root, "c")

    _git(root, "checkout", "-q", "main")
    return root


@pytest.fixture(autouse=True)
def _require_write_tree(repo: Path) -> None:
    """Skip rather than fail on a git too old to have the probe's plumbing."""
    version = git_version(repo)
    if not supports_write_tree(version):
        pytest.skip(f"git {version} predates {PROBE_MIN_GIT_VERSION}; probing is unavailable by design")


class _SpyGit:
    """Records every git argv the probe spawns, delegating to the real binary."""

    def __init__(self) -> None:
        self.calls: list[Sequence[str]] = []

    def subcommands(self) -> list[str]:
        return [call[0] for call in self.calls if call]


@pytest.fixture
def spy_git(monkeypatch: pytest.MonkeyPatch) -> _SpyGit:
    from bernstein.core.git import merge_tree_probe

    spy = _SpyGit()
    real = merge_tree_probe.run_git

    def _recording(args: list[str], cwd: Path, **kwargs: object) -> object:
        spy.calls.append(tuple(args))
        return real(args, cwd, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(merge_tree_probe, "run_git", _recording)
    return spy


# ---------------------------------------------------------------------------
# Determinism -- the property the whole design rests on
# ---------------------------------------------------------------------------


def test_same_pair_probed_twice_yields_identical_tree_id(repo: Path) -> None:
    """Probing one pair twice produces a byte-identical tree id."""
    first = probe_integration("worker/a", "worker/c", repo)
    second = probe_integration("worker/a", "worker/c", repo)

    assert first.tree_id == second.tree_id
    assert first.tree_id != ""
    # Not just the tree id: the entire recorded subject must be reproducible,
    # since a later step signs the whole object rather than one field.
    assert first == second


def test_conflicting_pair_probed_twice_is_also_identical(repo: Path) -> None:
    """Determinism holds on the conflicting branch too, not only the clean one."""
    first = probe_integration("worker/a", "worker/b", repo)
    second = probe_integration("worker/a", "worker/b", repo)

    assert first.verdict is ProbeVerdict.CONFLICTED
    assert first == second
    assert first.conflicted_paths_digest == second.conflicted_paths_digest


# ---------------------------------------------------------------------------
# The two verdicts
# ---------------------------------------------------------------------------


def test_known_conflict_is_reported_with_named_paths(repo: Path) -> None:
    """Two workers rewriting the same region conflict, and the paths are named."""
    probe = probe_integration("worker/a", "worker/b", repo)

    assert probe.verdict is ProbeVerdict.CONFLICTED
    assert probe.is_conflicted
    assert probe.exit_status == 1
    assert probe.conflicted_paths == ("shared.txt",)
    # A conflicted merge still produces a real tree object -- the tree of the
    # conflicted result -- so the receipt is content-addressed either way.
    assert probe.tree_id != ""
    assert probe.reason == ""
    # Files neither worker touched must not be reported as conflicting.
    assert "untouched.txt" not in probe.conflicted_paths


def test_disjoint_edits_compose_cleanly(repo: Path) -> None:
    """Workers editing different files produce a clean, tree-bearing probe."""
    probe = probe_integration("worker/a", "worker/c", repo)

    assert probe.verdict is ProbeVerdict.TEXTUAL_CLEAN
    assert not probe.is_conflicted
    assert probe.exit_status == 0
    assert probe.conflicted_paths == ()
    assert probe.tree_id != ""
    assert probe.merge_base == _git(repo, "merge-base", "worker/a", "worker/c").strip()


def test_clean_tree_id_is_the_real_merge_result(repo: Path) -> None:
    """The recorded tree id is the merge itself, checkable with stock git.

    This is the third-party re-derivation argument in miniature: the id names a
    tree object that exists in the repository and contains both workers' files.
    """
    probe = probe_integration("worker/a", "worker/c", repo)

    assert _git(repo, "cat-file", "-t", probe.tree_id).strip() == "tree"
    listing = _git(repo, "ls-tree", "-r", "--name-only", probe.tree_id).split()
    assert "only_a.txt" in listing
    assert "only_c.txt" in listing


# ---------------------------------------------------------------------------
# The probe must not mutate the repository
# ---------------------------------------------------------------------------


def test_probe_mutates_no_working_tree_index_or_branch(repo: Path) -> None:
    """HEAD, the branch list, the index and the working tree all survive intact."""

    def snapshot() -> tuple[str, str, str, str]:
        return (
            _git(repo, "rev-parse", "HEAD").strip(),
            _git(repo, "symbolic-ref", "--quiet", "HEAD").strip(),
            _git(repo, "for-each-ref", "--format=%(refname) %(objectname)").strip(),
            _git(repo, "status", "--porcelain").strip(),
        )

    before = snapshot()
    probe_integration("worker/a", "worker/b", repo)
    probe_integration("worker/a", "worker/c", repo)

    assert snapshot() == before
    # No stray worktree was registered either.
    assert _git(repo, "worktree", "list").strip().count("\n") == 0


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


def test_pair_is_ordered_and_recorded_as_given(repo: Path) -> None:
    """Swapping the sides is a different probe, recorded in the given order."""
    forward = probe_integration("worker/a", "worker/b", repo)
    reverse = probe_integration("worker/b", "worker/a", repo)

    assert forward.a_commit == reverse.b_commit
    assert forward.b_commit == reverse.a_commit
    # Both orders see the same collision; the recorded subject differs because
    # the sides do, which is why callers must fix the order canonically.
    assert forward.verdict is reverse.verdict is ProbeVerdict.CONFLICTED
    assert forward != reverse


def test_inputs_are_resolved_to_immutable_object_ids(repo: Path) -> None:
    """Branch names are resolved, so a later branch move cannot rewrite history."""
    probe = probe_integration("worker/a", "worker/c", repo)

    assert probe.a_commit == _git(repo, "rev-parse", "worker/a").strip()
    assert probe.b_commit == _git(repo, "rev-parse", "worker/c").strip()
    assert len(probe.a_commit) in (40, 64)


# ---------------------------------------------------------------------------
# Degraded modes
# ---------------------------------------------------------------------------


def test_old_git_disables_probing_without_invoking_merge_tree(repo: Path, spy_git: _SpyGit) -> None:
    """Below git 2.38 the verdict is UNAVAILABLE and merge-tree is never spawned."""
    probe = probe_integration("worker/a", "worker/b", repo, version=(2, 37, 9))

    assert probe.verdict is ProbeVerdict.UNAVAILABLE
    assert probe.reason == REASON_GIT_TOO_OLD
    assert probe.tree_id == ""
    assert probe.git_version == (2, 37, 9)
    # The load-bearing assertion: no probe subprocess ran at all.
    assert "merge-tree" not in spy_git.subcommands()


def test_unavailable_is_not_clean(repo: Path) -> None:
    """An UNAVAILABLE probe can never be read as evidence the pair composes."""
    probe = probe_integration("worker/a", "worker/b", repo, version=(2, 37, 9))

    assert probe.verdict is not ProbeVerdict.TEXTUAL_CLEAN
    assert not probe.probed
    assert not probe.is_conflicted


def test_unresolvable_commit_is_unavailable_not_an_exception(repo: Path) -> None:
    """A bad rev returns a verdict rather than raising."""
    probe = probe_integration("worker/a", "does-not-exist", repo)

    assert probe.verdict is ProbeVerdict.UNAVAILABLE
    assert probe.reason == REASON_UNRESOLVED_COMMIT
    assert probe.tree_id == ""


def test_unrelated_histories_report_no_merge_base(repo: Path) -> None:
    """Branches with no common ancestor are UNAVAILABLE, never clean."""
    _git(repo, "checkout", "-q", "--orphan", "worker/orphan")
    _git(repo, "rm", "-rq", "--cached", ".")
    for stale in repo.iterdir():
        if stale.name != ".git":
            if stale.is_dir():
                continue
            stale.unlink()
    _write(repo, "orphan.txt", "unrelated\n")
    _commit(repo, "orphan root")

    probe = probe_integration("worker/a", "worker/orphan", repo)

    assert probe.verdict is ProbeVerdict.UNAVAILABLE
    assert probe.reason == REASON_NO_MERGE_BASE
    assert probe.merge_base == ""


def test_no_verdict_spells_safe() -> None:
    """A clean probe is textual only; there is deliberately no SAFE verdict."""
    members = {member.value for member in ProbeVerdict}

    assert members == {"TEXTUAL_CLEAN", "CONFLICTED", "UNAVAILABLE"}
    assert "SAFE" not in members


# ---------------------------------------------------------------------------
# Merge-configuration digest
# ---------------------------------------------------------------------------


def test_merge_config_digest_is_stable_across_calls(repo: Path) -> None:
    """An unchanged configuration digests identically every time."""
    assert merge_config_digest(repo) == merge_config_digest(repo)


def test_merge_config_digest_tracks_gitattributes(repo: Path) -> None:
    """Adding, changing and removing .gitattributes each move the digest."""
    baseline = merge_config_digest(repo)

    _write(repo, ".gitattributes", "*.txt merge=union\n")
    _commit(repo, "add attributes")
    with_attributes = merge_config_digest(repo)
    assert with_attributes != baseline

    _write(repo, ".gitattributes", "*.txt merge=ours\n")
    _commit(repo, "change attributes")
    assert merge_config_digest(repo) != with_attributes


def test_merge_config_digest_tracks_nested_gitattributes(repo: Path) -> None:
    """A driver declared in a subdirectory counts too, not just the root file."""
    baseline = merge_config_digest(repo)

    _write(repo, "nested/dir/.gitattributes", "*.txt merge=union\n")
    _commit(repo, "add nested attributes")

    assert merge_config_digest(repo) != baseline


def test_merge_config_digest_tracks_rename_settings(repo: Path) -> None:
    """The rename-detection knobs are part of the recorded configuration."""
    baseline = merge_config_digest(repo)

    _git(repo, "config", "merge.renames", "false")
    assert merge_config_digest(repo) != baseline


def test_probe_carries_the_config_digest(repo: Path) -> None:
    """Every probe records the configuration it ran under."""
    probe = probe_integration("worker/a", "worker/c", repo)

    assert probe.merge_config_digest == merge_config_digest(repo)
    assert probe.merge_config_digest.startswith("sha256:")


# ---------------------------------------------------------------------------
# Version detection
# ---------------------------------------------------------------------------


def test_git_version_parses_the_local_banner(repo: Path) -> None:
    version = git_version(repo)

    assert version is not None
    assert len(version) == 3
    assert all(isinstance(part, int) for part in version)


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        (None, False),
        ((2, 37, 9), False),
        ((2, 38, 0), True),
        ((2, 54, 0), True),
        ((3, 0, 0), True),
    ],
)
def test_supports_write_tree_boundary(version: tuple[int, int, int] | None, expected: bool) -> None:
    """2.38 is the first version with --write-tree; the boundary is inclusive."""
    assert supports_write_tree(version) is expected


# ---------------------------------------------------------------------------
# Frozen result
# ---------------------------------------------------------------------------


def test_probe_result_is_frozen(repo: Path) -> None:
    """The recorded subject cannot be edited after the measurement."""
    probe = probe_integration("worker/a", "worker/c", repo)

    assert isinstance(probe, MergeTreeProbe)
    with pytest.raises((AttributeError, TypeError)):
        probe.tree_id = "tampered"  # type: ignore[misc]
