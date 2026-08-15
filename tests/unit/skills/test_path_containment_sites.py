"""Containment tests for the three ``core/skills`` path checks (issue #3824).

Each converted site gets an escape test paired with a positive control, so an
unconditional refusal cannot satisfy the suite. The escape vector is the one a
hand-rolled ``resolve()`` + ``is_relative_to()`` comparison is most likely to
miss: a *bucket directory that is itself a symlink* out of the skill tree.

The three sites did not agree on that vector before this change:

* ``lint.py`` refused it (it anchored containment to the unresolved bucket
  under the resolved skill root).
* ``lifecycle.py`` accepted it (it anchored to the *resolved* bucket, so the
  symlink was followed) and then raised an uncaught ``ValueError`` from its
  sort key, crashing ``compute_skill_digest``.
* ``packaging.py`` refused it (its base is the destination root, and the
  symlink sits in the candidate portion).

``lifecycle.py`` now matches ``lint.py``.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from bernstein.core.skills.lifecycle import compute_skill_digest
from bernstein.core.skills.lint import LintSeverity, lint_skill
from bernstein.core.skills.packaging import PackagedInstallError, _copy_tree


def _author(skill_dir: Path, name: str, *, references: list[str] | None = None) -> Path:
    """Write a minimal well-formed SKILL.md, optionally declaring references."""
    skill_dir.mkdir(parents=True, exist_ok=True)
    front = [
        "---",
        f"name: {name}",
        "description: A skill used by the path containment tests for this bucket.",
    ]
    if references:
        front.append("references:")
        front += [f"  - {entry}" for entry in references]
    front.append("---")
    (skill_dir / "SKILL.md").write_text(
        "\n".join(front)
        + textwrap.dedent(f"""

            # {name}

            Body text.
            """),
        encoding="utf-8",
    )
    return skill_dir


# --------------------------------------------------------------------------
# lifecycle.py - _referenced_files, reached through compute_skill_digest
# --------------------------------------------------------------------------


def test_digest_skips_reference_behind_symlinked_bucket(tmp_path: Path) -> None:
    """A symlinked ``references/`` must not pull host bytes into the digest.

    Before this change the resolved bucket root made the escape *pass*
    containment, and the file then failed the ``relative_to(skill_root)`` sort
    key with an uncaught ``ValueError``.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("host secret\n", encoding="utf-8")

    skill = _author(tmp_path / "skills" / "escaping", "escaping", references=["secret.md"])
    (skill / "references").symlink_to(outside, target_is_directory=True)

    # Does not raise, and the out-of-tree bytes do not participate.
    escaped = compute_skill_digest(skill)

    (outside / "secret.md").write_text("host secret, mutated\n", encoding="utf-8")
    assert compute_skill_digest(skill).digest == escaped.digest


def test_digest_includes_reference_under_real_bucket(tmp_path: Path) -> None:
    """Positive control: an ordinary in-tree reference still counts."""
    skill = _author(tmp_path / "skills" / "ordinary", "ordinary", references=["ref.md"])
    refs = skill / "references"
    refs.mkdir()
    (refs / "ref.md").write_text("ref body\n", encoding="utf-8")

    before = compute_skill_digest(skill)
    (refs / "ref.md").write_text("ref body, mutated\n", encoding="utf-8")
    assert compute_skill_digest(skill).digest != before.digest


@pytest.mark.parametrize("entry", ["..\\..\\escape.md", "", "."])
def test_digest_ignores_unusable_reference_entries(tmp_path: Path, entry: str) -> None:
    """Entries that name no path below the bucket are skipped, not crashed on.

    ``..\\..\\escape.md`` is the interesting one: ``Path(entry).parts`` keeps it
    as a single component on POSIX, so the previous ``".." in parts`` guard let
    it through. It is a parent reference wherever it is read.
    """
    skill = _author(tmp_path / "skills" / "odd", "odd", references=[entry])
    (skill / "references").mkdir()

    assert compute_skill_digest(skill).digest  # no exception


# --------------------------------------------------------------------------
# lint.py - reference bucket checks
# --------------------------------------------------------------------------


def test_lint_rejects_reference_behind_symlinked_bucket(tmp_path: Path) -> None:
    """The pre-existing refusal survives the conversion unchanged."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("host secret\n", encoding="utf-8")

    skill = _author(tmp_path / "skills" / "linted", "linted", references=["secret.md"])
    (skill / "references").symlink_to(outside, target_is_directory=True)

    findings = lint_skill(skill)
    assert any(f.code == "unsafe-reference-path" and f.severity is LintSeverity.ERROR for f in findings)


def test_lint_accepts_reference_under_real_bucket(tmp_path: Path) -> None:
    """Positive control: a real file under a real bucket lints clean."""
    skill = _author(tmp_path / "skills" / "clean", "clean", references=["ref.md"])
    refs = skill / "references"
    refs.mkdir()
    (refs / "ref.md").write_text("ref body\n", encoding="utf-8")

    codes = {f.code for f in lint_skill(skill)}
    assert "unsafe-reference-path" not in codes
    assert "missing-reference" not in codes


def test_lint_rejects_backslash_traversal_reference(tmp_path: Path) -> None:
    """A backslash parent reference is refused on POSIX too.

    The previous ``".." in Path(filename).parts`` guard did not see it, because
    POSIX does not split on backslash; the shared validator splits on both.
    """
    skill = _author(tmp_path / "skills" / "winpath", "winpath", references=["..\\..\\escape.md"])
    (skill / "references").mkdir()

    findings = lint_skill(skill)
    assert any(f.code == "unsafe-reference-path" and f.severity is LintSeverity.ERROR for f in findings)


# --------------------------------------------------------------------------
# packaging.py - _copy_tree
# --------------------------------------------------------------------------


def test_copy_tree_refuses_symlinked_destination_subdir(tmp_path: Path) -> None:
    """A symlinked directory inside the destination cannot capture the write."""
    source = tmp_path / "source"
    (source / "nested").mkdir(parents=True)
    (source / "nested" / "payload.txt").write_text("payload\n", encoding="utf-8")

    outside = tmp_path / "outside"
    outside.mkdir()

    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "nested").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PackagedInstallError, match="escapes destination"):
        _copy_tree(source, dest)
    assert not (outside / "payload.txt").exists()


def test_copy_tree_copies_ordinary_nested_files(tmp_path: Path) -> None:
    """Positive control: an ordinary nested tree still copies through."""
    source = tmp_path / "source"
    (source / "nested").mkdir(parents=True)
    (source / "nested" / "payload.txt").write_text("payload\n", encoding="utf-8")
    (source / "top.txt").write_text("top\n", encoding="utf-8")

    dest = tmp_path / "dest"
    _copy_tree(source, dest)

    assert (dest / "nested" / "payload.txt").read_text(encoding="utf-8") == "payload\n"
    assert (dest / "top.txt").read_text(encoding="utf-8") == "top\n"
