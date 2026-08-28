import json
import logging
from pathlib import Path

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


def test_a_test_that_failed_and_passed_without_a_source_change_is_flaky(tmp_path: Path):
    journal = tmp_path / "journal.json"
    runs = [
        {"test_id": "test_foo", "status": "failed", "commit": "abc"},
        {"test_id": "test_foo", "status": "passed", "commit": "abc"},  # Flaky
        {"test_id": "test_bar", "status": "failed", "commit": "abc"},
        {"test_id": "test_bar", "status": "passed", "commit": "def"},  # Not flaky (commit changed)
    ]
    journal.write_text(json.dumps(runs))

    flaky = get_known_flaky_tests([journal])
    assert "test_foo" in flaky
    assert "test_bar" not in flaky


def test_an_unreadable_journal_yields_no_flaky_list_and_a_logged_reason(tmp_path: Path, caplog):
    journal = tmp_path / "journal.json"
    journal.write_text("{bad_json")

    with caplog.at_level(logging.WARNING):
        flaky = get_known_flaky_tests([journal])

    assert flaky == []
    assert "Failed to read run journal" in caplog.text
