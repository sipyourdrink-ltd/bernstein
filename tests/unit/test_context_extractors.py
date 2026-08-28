from pathlib import Path

from bernstein.core.quality.flaky_detector import FlakyDetector
from bernstein.core.tasks.context_extractors import find_nearest_agents_md, get_known_flaky_tests

# --- AGENTS.md Extractor Tests ---


def test_the_nearest_agents_md_wins_over_an_ancestor(tmp_path: Path):
    (tmp_path / "AGENTS.md").write_text("Root agents")
    sub_dir = tmp_path / "src" / "module"
    sub_dir.mkdir(parents=True)
    (sub_dir / "AGENTS.md").write_text("Nested agents")

    target = sub_dir / "file.py"
    target.touch()

    assert find_nearest_agents_md(target, tmp_path) == "Nested agents"


def test_the_agents_md_is_verbatim(tmp_path: Path):
    content = "Line 1\n\nLine 2 with trailing space \n"
    (tmp_path / "AGENTS.md").write_text(content)

    assert find_nearest_agents_md(tmp_path / "target.py", tmp_path) == content


def test_a_target_with_no_agents_md_anywhere_above_it_is_not_an_error(tmp_path: Path):
    sub_dir = tmp_path / "src"
    sub_dir.mkdir()

    assert find_nearest_agents_md(sub_dir / "target.py", tmp_path) is None


# --- Flaky Test Extractor Tests ---


def test_the_extractor_reports_what_the_gate_deselects(tmp_path: Path):
    """The prompt must name the same tests the gate is skipping.

    Seeded through ``FlakyDetector`` rather than by writing the file by
    hand, so the test breaks if the quarantine ever moves or changes shape
    instead of silently reporting an empty list.
    """
    detector = FlakyDetector(tmp_path)
    detector._write_quarantine(["tests/test_b.py::test_two", "tests/test_a.py::test_one"])

    assert get_known_flaky_tests(tmp_path) == [
        "tests/test_a.py::test_one",
        "tests/test_b.py::test_two",
    ]


def test_a_workdir_with_no_quarantine_yet_is_not_an_error(tmp_path: Path):
    assert get_known_flaky_tests(tmp_path) == []
