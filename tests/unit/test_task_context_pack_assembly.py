"""Assembly of the task context pack (#4522).

The extractors already exist and are exercised on their own. What these
tests pin is the assembly point: turning the four evidence sources into one
bounded, content-addressed pack whose hash the spawn receipt records, and
which adds nothing at all to the prompt until an operator turns it on.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from bernstein.core.agents.context_receipt import build_context_receipt
from bernstein.core.quality.flaky_detector import FlakyDetector
from bernstein.core.tasks.context_pack import (
    CONTEXT_PACK_FLAG,
    PACK_SECTION_LABEL,
    assemble_context_pack,
    context_pack_enabled,
    render_pack_section,
)

SRC_DIR = str(Path(__file__).resolve().parent.parent.parent / "src")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ("git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=test@example.com", *args),
        check=True,
        stdout=subprocess.DEVNULL,
    )


def _commit(repo: Path, message: str, *paths: str) -> None:
    for path in paths:
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(message, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", message)


def _fixture_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init")
    _commit(root, "model and schema", "src/model.py", "schemas/model.json", "tests/test_model.py")
    _commit(root, "model and gate", "src/model.py", "src/gate.py")
    (root / "src" / "AGENTS.md").write_text("Keep this module under 40 lines.\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "nested agents file")
    FlakyDetector(root)._write_quarantine(["tests/test_slow.py::test_timer"])
    return root


# 1 -------------------------------------------------------------------------
def test_an_assembled_pack_rebuilds_byte_identically(tmp_path: Path) -> None:
    """Two assemblies over the same repository state must agree byte for byte.

    Run in two separate interpreters under different hash seeds: determinism
    that only holds inside one process is leaning on dict insertion order,
    which is exactly the bug a content-addressed pack cannot survive.
    """
    repo = _fixture_repo(tmp_path / "repo")
    script = tmp_path / "build.py"
    script.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "from bernstein.core.tasks.context_pack import assemble_context_pack\n"
        "pack = assemble_context_pack(Path(sys.argv[1]), ['src/model.py', 'src/gate.py'])\n"
        "sys.stdout.buffer.write(pack.canonical_bytes())\n",
        encoding="utf-8",
    )

    outputs = []
    for seed in ("1", "2"):
        env = os.environ.copy()
        env["PYTHONHASHSEED"] = seed
        env["PYTHONPATH"] = SRC_DIR
        outputs.append(
            subprocess.run([sys.executable, str(script), str(repo)], capture_output=True, env=env, check=True).stdout
        )

    assert outputs[0] == outputs[1]
    assert b"schemas/model.json" in outputs[0]


# 2 -------------------------------------------------------------------------
def test_target_ordering_does_not_change_the_pack(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path / "repo")
    forward = assemble_context_pack(repo, ["src/model.py", "src/gate.py"])
    reversed_ = assemble_context_pack(repo, ["src/gate.py", "src/model.py", "src/gate.py"])
    assert forward.canonical_bytes() == reversed_.canonical_bytes()
    assert forward.content_address() == reversed_.content_address()


# 3 -------------------------------------------------------------------------
def test_a_tampered_entry_no_longer_matches_the_recorded_hash(tmp_path: Path) -> None:
    """The spawn receipt is the run record; a doctored pack must not verify."""
    repo = _fixture_repo(tmp_path / "repo")
    pack = assemble_context_pack(repo, ["src/model.py"])
    recorded = build_context_receipt([(PACK_SECTION_LABEL, pack.render())])
    recorded_sha = recorded.entries[0].content_sha256

    tampered = pack.with_sections(
        tuple(
            section.replace_items(("attacker/controlled.py",)) if section.items else section
            for section in pack.sections
        )
    )
    recomputed = build_context_receipt([(PACK_SECTION_LABEL, tampered.render())])

    assert tampered.content_address() != pack.content_address()
    assert recomputed.entries[0].content_sha256 != recorded_sha
    # Re-deriving the untampered pack offline still reproduces the record.
    assert (
        build_context_receipt([(PACK_SECTION_LABEL, assemble_context_pack(repo, ["src/model.py"]).render())])
        .entries[0]
        .content_sha256
        == recorded_sha
    )


# 4 -------------------------------------------------------------------------
def test_a_truncated_list_says_so_inside_the_pack(tmp_path: Path) -> None:
    """A cut list that stays silent reads as 'there was nothing else'."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    neighbours = [f"src/n{index:02d}.py" for index in range(12)]
    _commit(repo, "wide change", "src/hub.py", *neighbours)

    pack = assemble_context_pack(repo, ["src/hub.py"], item_limit=3)
    payload = json.loads(pack.canonical_bytes())
    co_change = next(s for s in payload["sections"] if s["kind"] == "co_change")

    assert len(co_change["items"]) == 3
    assert co_change["truncated"] == {"kept": 3, "available": 12}
    assert "9 more not shown" in pack.render()


def test_a_section_dropped_for_the_byte_budget_says_so_inside_the_pack(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path / "repo")
    pack = assemble_context_pack(repo, ["src/model.py", "src/gate.py"], byte_budget=400)

    assert len(pack.canonical_bytes()) <= 400
    assert pack.dropped, "a pack cut to fit the budget must name what it dropped"
    assert json.loads(pack.canonical_bytes())["dropped"] == sorted(pack.dropped)


def test_the_budget_evicts_the_commit_graph_lists_before_the_local_invariants(tmp_path: Path) -> None:
    """What survives a tight budget is a decision, not an accident of order.

    The invariants an agent is most likely to break, and the tests the gate is
    already skipping, are short and high-value; the co-change list is long and
    the cheapest thing to rediscover. A budget that evicts in arrival order
    would drop the first of those and keep the last.
    """
    repo = _fixture_repo(tmp_path / "repo")
    pack = assemble_context_pack(repo, ["src/model.py", "src/gate.py"], byte_budget=400)

    kinds = {section.kind for section in pack.sections}
    assert "agents_md" in kinds
    assert "co_change" not in kinds
    assert any(key.startswith("co_change:") for key in pack.dropped)


# 5 -------------------------------------------------------------------------
def test_missing_history_and_missing_agents_md_yield_a_smaller_pack_not_a_failure(tmp_path: Path) -> None:
    empty = tmp_path / "not-a-repo"
    empty.mkdir()
    pack = assemble_context_pack(empty, ["src/model.py"])
    assert pack.is_empty()
    assert pack.render() == ""


def test_an_unreadable_journal_is_a_logged_reason_not_a_failed_spawn(tmp_path: Path, monkeypatch) -> None:
    repo = _fixture_repo(tmp_path / "repo")

    def _boom(_workdir):
        raise OSError("quarantine unreadable")

    monkeypatch.setattr("bernstein.core.tasks.context_pack.get_known_flaky_tests", _boom)
    pack = assemble_context_pack(repo, ["src/model.py"])

    assert "flaky_tests" in pack.unavailable
    assert not pack.is_empty(), "one broken source must not empty the whole pack"


# 6 -------------------------------------------------------------------------
def test_the_pack_is_off_unless_the_flag_is_set(tmp_path: Path, monkeypatch) -> None:
    repo = _fixture_repo(tmp_path / "repo")
    monkeypatch.delenv(CONTEXT_PACK_FLAG, raising=False)
    assert context_pack_enabled() is False
    assert render_pack_section(repo, ["src/model.py"]) == ""

    monkeypatch.setenv(CONTEXT_PACK_FLAG, "1")
    assert context_pack_enabled() is True
    assert "schemas/model.json" in render_pack_section(repo, ["src/model.py"])


# 7 -------------------------------------------------------------------------
def test_an_empty_pack_leaves_the_spawn_prompt_byte_identical(tmp_path: Path, monkeypatch) -> None:
    """Turning the flag on over a repository with no history must change nothing."""
    from bernstein.core.agents.spawner_core import _render_prompt_with_receipt
    from bernstein.core.tasks.models import Task

    workdir = tmp_path / "workdir"
    workdir.mkdir()
    task = Task(id="t1", title="Do the thing", description="Body", role="backend")
    task.owned_files = ["src/model.py"]

    monkeypatch.delenv(CONTEXT_PACK_FLAG, raising=False)
    without, _ = _render_prompt_with_receipt([task], tmp_path / "templates", workdir)

    monkeypatch.setenv(CONTEXT_PACK_FLAG, "1")
    with_flag, receipt = _render_prompt_with_receipt([task], tmp_path / "templates", workdir)

    assert with_flag == without
    assert PACK_SECTION_LABEL not in {entry.label for entry in receipt.entries}


def test_a_non_empty_pack_reaches_the_prompt_and_the_receipt(tmp_path: Path, monkeypatch) -> None:
    from bernstein.core.agents.spawner_core import _render_prompt_with_receipt
    from bernstein.core.tasks.models import Task

    workdir = _fixture_repo(tmp_path / "repo")
    task = Task(id="t1", title="Do the thing", description="Body", role="backend")
    task.owned_files = ["src/model.py"]

    monkeypatch.setenv(CONTEXT_PACK_FLAG, "1")
    prompt, receipt = _render_prompt_with_receipt([task], tmp_path / "templates", workdir)

    assert "schemas/model.json" in prompt
    assert PACK_SECTION_LABEL in {entry.label for entry in receipt.entries}


@pytest.mark.parametrize("raw", ["", "0", "off", "false", "no", " "])
def test_only_an_explicit_truthy_flag_turns_the_pack_on(monkeypatch, raw: str) -> None:
    monkeypatch.setenv(CONTEXT_PACK_FLAG, raw)
    assert context_pack_enabled() is False
