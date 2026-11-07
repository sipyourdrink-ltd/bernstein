"""A plan step's declared completion signals survive to the janitor, and pass on correct work (#5960).

Two halves were each correct and the whole was not. The planner now forwards a
step's `completion_signals` to the task server, but the plan loader collapsed the
documented `file_contains` form, `{type, path, contains}`, to the path alone, and
the janitor's `_check_file_contains` returns False for any value without
`" :: "`. Forwarded, that turned a correctly written step into a task that fails
on correct work. These tests follow one plan from YAML to `evaluate_signal`, so
"the declared witnesses survive to be evaluated" is something CI checks.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import httpx
import pytest
import yaml

from bernstein.core.planning import planner
from bernstein.core.planning.plan_loader import _MAX_COMPLETION_SIGNALS, PlanLoadError, load_plan
from bernstein.core.quality.janitor import evaluate_signal
from bernstein.core.server import create_app
from bernstein.core.server.server_models import _MAX_LIST_LEN  # pyright: ignore[reportPrivateUsage]

if TYPE_CHECKING:
    from pathlib import Path


def _plan(tmp_path: Path, step: dict[str, object]) -> Path:
    path = tmp_path / "plan.yaml"
    path.write_text(
        yaml.safe_dump({"name": "p", "stages": [{"name": "S", "steps": [{"title": "T", **step}]}]}),
        encoding="utf-8",
    )
    return path


def _post_and_store(tmp_path: Path, plan_file: Path) -> list[object]:
    """load_plan, POST each task to the real app, and return what the server stored."""
    app = create_app(jsonl_path=tmp_path / ".sdd" / "runtime" / "tasks.jsonl")
    _, tasks = load_plan(plan_file)

    async def _post_all() -> list[str]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://server") as client:
            return [await planner._post_task_to_server(client, "http://server", task) for task in tasks]

    ids = asyncio.run(_post_all())
    stored = [app.state.store.get_task(task_id) for task_id in ids]
    assert all(task is not None for task in stored)
    return stored


@pytest.mark.parametrize(
    "signal",
    [
        {"type": "file_contains", "path": "README.md", "contains": "Usage"},
        {"type": "file_contains", "value": "README.md :: Usage"},
    ],
    ids=["documented-path-contains", "precomposed-value"],
)
def test_a_documented_file_contains_passes_on_correct_work(tmp_path: Path, signal: dict[str, str]) -> None:
    work = tmp_path / "work"
    work.mkdir()
    (work / "README.md").write_text("# Project\n\n## Usage\n", encoding="utf-8")

    (task,) = _post_and_store(tmp_path, _plan(tmp_path, {"completion_signals": [signal]}))

    (stored,) = task.completion_signals  # type: ignore[attr-defined]
    assert stored.type == "file_contains"
    assert stored.value == "README.md :: Usage"
    passed, detail = evaluate_signal(stored, work)
    assert passed, detail


def test_and_still_fails_when_the_text_is_not_there(tmp_path: Path) -> None:
    """The negative control: composing the spec must not make the check vacuous."""
    work = tmp_path / "work"
    work.mkdir()
    (work / "README.md").write_text("# Project\n", encoding="utf-8")
    signal = {"type": "file_contains", "path": "README.md", "contains": "Usage"}

    (task,) = _post_and_store(tmp_path, _plan(tmp_path, {"completion_signals": [signal]}))

    passed, _ = evaluate_signal(task.completion_signals[0], work)  # type: ignore[attr-defined]
    assert not passed


@pytest.mark.parametrize(
    "signal",
    [
        {"type": "file_contains", "path": "README.md"},
        {"type": "file_contains", "contains": "Usage"},
        {"type": "file_contains", "value": "README.md"},
    ],
    ids=["no-contains", "no-path", "value-without-separator"],
)
def test_a_file_contains_the_janitor_could_never_pass_is_refused_at_load(
    tmp_path: Path, signal: dict[str, str]
) -> None:
    with pytest.raises(PlanLoadError, match="file_contains"):
        load_plan(_plan(tmp_path, {"completion_signals": [signal]}))


def test_an_artifact_spec_with_an_llm_judge_is_refused_at_load_not_mid_run(tmp_path: Path) -> None:
    """The task server refuses the pair; found at POST time, earlier steps already existed."""
    step = {
        "artifact_spec": {"kind": "report", "output_path": "docs/report.md"},
        "completion_signals": [{"type": "llm_judge", "value": "the report answers the question"}],
    }
    with pytest.raises(PlanLoadError, match="llm_judge"):
        load_plan(_plan(tmp_path, step))


def test_an_llm_judge_on_the_default_contract_still_loads(tmp_path: Path) -> None:
    """The planner posts no artifact_spec for the default code_diff contract, so no pair."""
    step = {"completion_signals": [{"type": "llm_judge", "value": "the change is correct"}]}
    _, tasks = load_plan(_plan(tmp_path, step))
    assert tasks[0].completion_signals[0].type == "llm_judge"


def test_more_signals_than_the_server_accepts_is_refused_at_load(tmp_path: Path) -> None:
    signals = [{"type": "path_exists", "path": f"f{i}.py"} for i in range(_MAX_COMPLETION_SIGNALS + 1)]
    with pytest.raises(PlanLoadError, match="completion_signals"):
        load_plan(_plan(tmp_path, {"completion_signals": signals}))


def test_the_signal_cap_is_the_task_server_s() -> None:
    assert _MAX_COMPLETION_SIGNALS == _MAX_LIST_LEN
