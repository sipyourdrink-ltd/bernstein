"""Every path a unified diff touches, including the ones with no hunk.

Each test is named for the property it protects rather than for the input it
uses. The property under all of them is one-directional: this extractor may
report a path the diff does not really touch, and may never fail to report one
it does, because two security boundaries fail open on a missed path.
"""

from __future__ import annotations

from bernstein.core.diff_paths import extract_paths_from_unified_diff


def test_an_ordinary_edit_surfaces_its_path_once() -> None:
    diff = (
        "diff --git a/src/pkg/mod.py b/src/pkg/mod.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/src/pkg/mod.py\n"
        "+++ b/src/pkg/mod.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    assert extract_paths_from_unified_diff(diff) == ("src/pkg/mod.py",)


def test_a_content_preserving_rename_surfaces_both_paths_without_any_hunk() -> None:
    """A 100%-similarity rename prints no ``---``/``+++`` pair at all.

    Reading only hunk headers would report nothing for it, so a file could be
    moved out of a protected scope and the check would see an empty patch.
    """
    diff = (
        "diff --git a/docs/guide.md b/src/pkg/guide.md\n"
        "similarity index 100%\n"
        "rename from docs/guide.md\n"
        "rename to src/pkg/guide.md\n"
    )
    assert extract_paths_from_unified_diff(diff) == ("docs/guide.md", "src/pkg/guide.md")


def test_a_copy_surfaces_both_paths_without_any_hunk() -> None:
    diff = (
        "diff --git a/docs/guide.md b/docs/copy.md\n"
        "similarity index 100%\n"
        "copy from docs/guide.md\n"
        "copy to docs/copy.md\n"
    )
    assert extract_paths_from_unified_diff(diff) == ("docs/guide.md", "docs/copy.md")


def test_a_mode_change_alone_surfaces_its_path() -> None:
    """``chmod +x`` prints no hunk, and is a change worth refusing on."""
    diff = "diff --git a/scripts/run.sh b/scripts/run.sh\nold mode 100644\nnew mode 100755\n"
    assert extract_paths_from_unified_diff(diff) == ("scripts/run.sh",)


def test_a_binary_change_surfaces_its_path() -> None:
    diff = (
        "diff --git a/assets/logo.png b/assets/logo.png\n"
        "index 3333333..4444444 100644\n"
        "Binary files a/assets/logo.png and b/assets/logo.png differ\n"
    )
    assert extract_paths_from_unified_diff(diff) == ("assets/logo.png",)


def test_a_pure_deletion_surfaces_the_old_side_path() -> None:
    """The new side is ``/dev/null``; the deleted file is on the old side."""
    diff = (
        "diff --git a/src/pkg/gone.py b/src/pkg/gone.py\n"
        "deleted file mode 100644\n"
        "--- a/src/pkg/gone.py\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n"
        "-content\n"
    )
    assert extract_paths_from_unified_diff(diff) == ("src/pkg/gone.py",)


def test_dev_null_is_never_reported_as_a_path() -> None:
    """Reporting it would refuse every added and every deleted file."""
    diff = "--- /dev/null\n+++ b/src/pkg/new.py\n@@ -0,0 +1 @@\n+content\n"
    assert extract_paths_from_unified_diff(diff) == ("src/pkg/new.py",)


def test_a_c_quoted_non_ascii_path_is_decoded_to_the_name_on_disk() -> None:
    """Git quotes non-ASCII names; the undecoded token matches no glob.

    Leaving it quoted would refuse every patch touching a translated file,
    which reads as a scope bug and is a decoding bug.
    """
    diff = 'diff --git "a/docs/caf\\303\\251.md" "b/docs/caf\\303\\251.md"\nold mode 100644\nnew mode 100755\n'
    assert extract_paths_from_unified_diff(diff) == ("docs/café.md",)


def test_a_quoted_hunk_header_is_decoded_the_same_way() -> None:
    diff = '--- "a/docs/caf\\303\\251.md"\n+++ "b/docs/caf\\303\\251.md"\n@@ -1 +1 @@\n-a\n+b\n'
    assert extract_paths_from_unified_diff(diff) == ("docs/café.md",)


def test_a_no_prefix_diff_surfaces_its_path_without_losing_a_segment() -> None:
    diff = "diff --git src/pkg/mod.py src/pkg/mod.py\nold mode 100644\nnew mode 100755\n"
    assert extract_paths_from_unified_diff(diff) == ("src/pkg/mod.py",)


def test_a_path_containing_the_b_side_marker_still_surfaces_intact() -> None:
    """``a/x b/y.sh b/x b/y.sh`` splits ambiguously; every candidate is kept.

    The true path has to be among the candidates: dropping it because the
    header was ambiguous is the failure mode this module refuses to have.
    """
    diff = "diff --git a/x b/y.sh b/x b/y.sh\nold mode 100644\nnew mode 100755\n"
    assert "x b/y.sh" in extract_paths_from_unified_diff(diff)


def test_a_forged_header_inside_content_adds_a_path_rather_than_hiding_one() -> None:
    """A ``+`` line that looks like a header is read as one, on purpose.

    An extra path costs a refusal the author can read. A parser clever enough
    to skip "content" headers would be one bug away from skipping a real one.
    """
    diff = (
        "diff --git a/docs/guide.md b/docs/guide.md\n"
        "--- a/docs/guide.md\n"
        "+++ b/docs/guide.md\n"
        "@@ -1 +1,2 @@\n"
        " intro\n"
        "+++ b/src/pkg/smuggled.py\n"
    )
    paths = extract_paths_from_unified_diff(diff)
    assert paths == ("docs/guide.md", "src/pkg/smuggled.py")


def test_paths_are_deduplicated_in_first_seen_order() -> None:
    diff = (
        "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n-x\n+y\n"
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n"
    )
    assert extract_paths_from_unified_diff(diff) == ("b.py", "a.py")


def test_an_empty_diff_names_nothing() -> None:
    assert extract_paths_from_unified_diff("") == ()
    assert extract_paths_from_unified_diff("\n\n") == ()
