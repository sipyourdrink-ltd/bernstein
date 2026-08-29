from __future__ import annotations

import subprocess
from pathlib import Path

from bernstein.core.tasks.context_extractors import extract_test_to_source_map


def _commit(repo: Path, message: str, *paths: str) -> None:
    for path in paths:
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(message, encoding="utf-8")
    subprocess.run(("git", "-C", str(repo), "add", "."), check=True)
    subprocess.run(
        ("git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", message),
        check=True,
        stdout=subprocess.DEVNULL,
    )


def test_a_test_that_never_co_changed_is_not_offered(tmp_path: Path) -> None:
    subprocess.run(("git", "-C", str(tmp_path), "init"), check=True, stdout=subprocess.DEVNULL)
    _commit(tmp_path, "green source change", "src/a.py", "tests/test_a.py")
    _commit(tmp_path, "green unrelated test", "tests/test_never.py")
    assert extract_test_to_source_map(tmp_path, ["src/a.py"]) == {"src/a.py": ["tests/test_a.py"]}


def test_only_commits_that_landed_green_contribute(tmp_path: Path) -> None:
    subprocess.run(("git", "-C", str(tmp_path), "init"), check=True, stdout=subprocess.DEVNULL)
    _commit(tmp_path, "red source change", "src/a.py", "tests/test_red.py")
    _commit(tmp_path, "green source change", "src/a.py", "tests/test_green.py")
    assert extract_test_to_source_map(tmp_path, ["src/a.py"]) == {"src/a.py": ["tests/test_green.py"]}


def test_the_map_is_stable_under_input_reordering(tmp_path: Path) -> None:
    subprocess.run(("git", "-C", str(tmp_path), "init"), check=True, stdout=subprocess.DEVNULL)
    _commit(tmp_path, "green source change", "src/a.py", "tests/test_a.py")
    _commit(tmp_path, "green source change", "src/b.py", "tests/test_b.py")
    assert extract_test_to_source_map(tmp_path, ["src/a.py", "src/b.py"]) == extract_test_to_source_map(
        tmp_path, ["src/b.py", "src/a.py"]
    )


def test_a_target_with_no_history_yields_an_empty_map_and_no_error(tmp_path: Path) -> None:
    subprocess.run(("git", "-C", str(tmp_path), "init"), check=True, stdout=subprocess.DEVNULL)
    assert extract_test_to_source_map(tmp_path, ["src/missing.py"]) == {"src/missing.py": []}
