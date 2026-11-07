"""The incident corpus resolves to one directory for the writer and every reader.

The synthesiser runs against whatever project the orchestrator manages, so the
corpus location is resolved per project: Bernstein's own source tree keeps
extending its checked-in ``src/bernstein/eval/cases/incidents/``, and every
other project keeps its cases under ``.sdd/eval/cases/incidents/`` with the
rest of the run state. The P0 gate and the ``incident:`` diagnose signal must
read the directory the synthesiser writes, in every layout.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bernstein.core.replay.diagnose_signals import resolve_signal
from bernstein.core.tasks.dead_letter_queue import DeadLetterQueue
from bernstein.eval.incident_synthesizer import (
    IncidentSynthesizer,
    incident_cases_dir,
    run_incident_eval_gate,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_CHECKED_IN = ("src", "bernstein", "eval", "cases", "incidents")
_RUN_STATE = (".sdd", "eval", "cases", "incidents")
_LEFTOVER_CASE_ID = "inc-000000000000"


def _managed_project(root: Path) -> Path:
    """A project with its own source tree and no Bernstein package in it."""
    package = root / "src" / "product"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    return root


def _managed_project_with_leftover_corpus(root: Path) -> Path:
    """A managed project holding the ``src/`` corpus an earlier version wrote."""
    _managed_project(root)
    leftover = root.joinpath(*_CHECKED_IN)
    leftover.mkdir(parents=True)
    (leftover / f"{_LEFTOVER_CASE_ID}.yaml").write_text(
        f"id: {_LEFTOVER_CASE_ID}\nseverity: P0\n",
        encoding="utf-8",
    )
    return root


def _bernstein_source_tree(root: Path) -> Path:
    """Bernstein's own checkout: the package beside its checked-in corpus."""
    root.joinpath(*_CHECKED_IN).mkdir(parents=True)
    (root / "src" / "bernstein" / "__init__.py").write_text("", encoding="utf-8")
    return root


def _seed_p0_incident(workdir: Path) -> None:
    DeadLetterQueue(sdd_dir=workdir / ".sdd").enqueue(
        task_id="T-1",
        title="Handle untrusted tool output",
        role="backend",
        reason="prompt_injection detected",
        retry_count=3,
        original_error="InjectionDetected: tool output asked the agent to disable its sandbox",
        metadata={"tags": ["prompt_injection"]},
    )


def _source_tree(workdir: Path) -> list[Path]:
    return sorted((workdir / "src").rglob("*"))


@pytest.mark.parametrize(
    "make_project",
    [
        pytest.param(_managed_project, id="managed-project"),
        pytest.param(_managed_project_with_leftover_corpus, id="leftover-src-corpus"),
    ],
)
def test_managed_project_cases_stay_out_of_its_source_tree(
    tmp_path: Path,
    make_project: Callable[[Path], Path],
) -> None:
    """A dead-lettered task in a managed project lands under ``.sdd/``, never ``src/``.

    A ``src/bernstein/eval/cases/incidents/`` directory alone does not make a
    project Bernstein's source tree, so one an earlier version left behind is
    not extended either.
    """
    workdir = make_project(tmp_path)
    source_before = _source_tree(workdir)
    _seed_p0_incident(workdir)

    result = IncidentSynthesizer(workdir).sync()

    assert len(result.created) == 1
    assert incident_cases_dir(workdir) == workdir.joinpath(*_RUN_STATE)
    written = sorted(workdir.joinpath(*_RUN_STATE).glob("inc-*.yaml"))
    assert [p.stem for p in written] == [result.created[0].id]
    assert _source_tree(workdir) == source_before


def test_bernstein_source_tree_keeps_its_checked_in_corpus(tmp_path: Path) -> None:
    """Bernstein's own checkout keeps extending the corpus it checks in."""
    workdir = _bernstein_source_tree(tmp_path)
    _seed_p0_incident(workdir)

    result = IncidentSynthesizer(workdir).sync()

    assert len(result.created) == 1
    written = sorted(workdir.joinpath(*_CHECKED_IN).glob("inc-*.yaml"))
    assert [p.stem for p in written] == [result.created[0].id]
    assert not workdir.joinpath(*_RUN_STATE).exists()


@pytest.mark.parametrize(
    "make_project",
    [
        pytest.param(_managed_project, id="managed-project"),
        pytest.param(_managed_project_with_leftover_corpus, id="leftover-src-corpus"),
        pytest.param(_bernstein_source_tree, id="bernstein-source-tree"),
    ],
)
def test_gate_and_diagnose_read_the_case_the_synthesizer_wrote(
    tmp_path: Path,
    make_project: Callable[[Path], Path],
) -> None:
    """The P0 gate and ``--signal incident:`` see exactly the synthesiser's corpus."""
    workdir = make_project(tmp_path)
    _seed_p0_incident(workdir)
    case = IncidentSynthesizer(workdir).sync().created[0]

    passed, detail, counts = run_incident_eval_gate(workdir)

    # One unproven P0 case: the one just written, and not the leftover.
    assert passed is False
    assert counts == {"P0": 1, "P1": 0, "P2": 0}
    assert case.id in detail

    # The audit CLI passes only the .sdd directory; the project root is its parent.
    predicate = resolve_signal(f"incident:{case.id}", sdd_dir=workdir / ".sdd")
    assert predicate.params["case_id"] == case.id


def test_explicit_cases_dir_overrides_the_resolved_corpus(tmp_path: Path) -> None:
    """A caller-supplied ``cases_dir`` wins for the writer and for diagnose."""
    workdir = _bernstein_source_tree(tmp_path)
    override = tmp_path / "corpus-override"
    _seed_p0_incident(workdir)

    case = IncidentSynthesizer(workdir, cases_dir=override).sync().created[0]

    assert [p.stem for p in override.glob("inc-*.yaml")] == [case.id]
    assert not any(workdir.joinpath(*_CHECKED_IN).iterdir())
    predicate = resolve_signal(f"incident:{case.id}", sdd_dir=workdir / ".sdd", cases_dir=override)
    assert predicate.params["case_id"] == case.id
