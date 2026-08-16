"""Structural assertions for the fixed critical-path mutation gate.

``mutation-fixed.yml`` shipped 2026-05-17 with a blanket
``continue-on-error: true`` on the ``mutate`` job and a shell fallback
(``|| echo ...``) in the harness step that absorbed the harness's own
exit code. Together the two meant the job - and so the run - reported
``success`` no matter what ``scripts/mutmut_critical.py`` found. The
comment called it advisory "for the first two weeks"; nothing revisited
it for 13 (issue #3953).

Pulling the kill-rate history off a recent scheduled run showed six of
the seven modules hold their threshold with real margin. ``audit_log``
does not: 32/80 killed against a 70% threshold, a 30-point gap that
persisted across every archived run, not sampling noise. This module
pins the resulting split - most modules gate for real, one stays
advisory by a reviewed, closed exception with the data that justified
it - so the exception can't grow by omission.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "mutation-fixed.yml"

# Modules still below their gate threshold. Closed list, reviewed in code:
# adding a module here without also raising its kill rate (or lowering its
# threshold in scripts/mutmut_critical.py:MODULES with a documented reason)
# defeats the gate the same way the blanket continue-on-error used to.
ADVISORY_MODULES: set[str] = {"audit_log"}


def _load() -> dict[object, object]:
    return cast("dict[object, object]", yaml.safe_load(WORKFLOW.read_text(encoding="utf-8")))


def _mutate_job() -> dict[str, object]:
    jobs = _load().get("jobs")
    assert isinstance(jobs, dict), "mutation-fixed.yml must declare jobs"
    job = jobs.get("mutate")
    assert isinstance(job, dict), "mutation-fixed.yml must declare a `mutate` job"
    return cast("dict[str, object]", job)


def _matrix() -> dict[str, object]:
    strategy = _mutate_job().get("strategy")
    assert isinstance(strategy, dict), "the mutate job must be matrix-driven"
    matrix = strategy.get("matrix")
    assert isinstance(matrix, dict)
    return cast("dict[str, object]", matrix)


def _matrix_modules() -> list[str]:
    modules = _matrix().get("module")
    assert isinstance(modules, list) and modules, "matrix.module must list the gated modules"
    return cast("list[str]", modules)


def _matrix_include() -> list[dict[str, object]]:
    include = _matrix().get("include")
    assert isinstance(include, list) and include, (
        "matrix needs an `include` block carrying the per-module advisory flag"
    )
    return cast("list[dict[str, object]]", include)


def _harness_step() -> dict[str, object]:
    steps = _mutate_job().get("steps")
    assert isinstance(steps, list)
    matches = [s for s in steps if s.get("name") == "Run focused mutation harness"]
    assert len(matches) == 1, "expected exactly one 'Run focused mutation harness' step"
    return cast("dict[str, object]", matches[0])


def _upload_step() -> dict[str, object]:
    steps = _mutate_job().get("steps")
    assert isinstance(steps, list)
    matches = [s for s in steps if s.get("name") == "Upload module result"]
    assert len(matches) == 1, "expected exactly one 'Upload module result' step"
    return cast("dict[str, object]", matches[0])


def test_continue_on_error_is_matrix_driven_not_blanket() -> None:
    """The job-level flag must read per-module state, not a literal `true`.

    A literal `true` is the exact defect from #3953: every module opts out
    of the run conclusion regardless of its own kill rate.
    """
    coe = _mutate_job().get("continue-on-error")
    assert coe not in (True, "true"), (
        "continue-on-error is a blanket literal again; a real kill-rate "
        "regression on any module would render as a green check"
    )
    assert isinstance(coe, str) and "matrix" in coe, (
        "continue-on-error must read a per-module matrix value so only "
        "modules below threshold can opt out of the run conclusion"
    )


def test_advisory_exception_list_matches_the_pinned_set() -> None:
    """The matrix `include` advisory flags must match ADVISORY_MODULES exactly.

    Every module not in ADVISORY_MODULES must set advisory: false, i.e. it
    genuinely gates. This is what keeps a module from staying advisory
    past the point its own data justifies it, or a new advisory module
    from sneaking in unreviewed.
    """
    modules = set(_matrix_modules())
    seen: dict[object, bool] = {}
    for entry in _matrix_include():
        module = entry.get("module")
        assert module in modules, f"include entry for unknown module {module!r}"
        assert "advisory" in entry, f"include entry for {module!r} does not set advisory"
        advisory = entry["advisory"]
        assert isinstance(advisory, bool), f"{module!r}'s advisory flag must be a real boolean, got {advisory!r}"
        seen[module] = advisory

    assert set(seen) == modules, f"every matrix module needs an include entry; missing {modules - set(seen)}"

    actually_advisory = {m for m, advisory in seen.items() if advisory}
    assert actually_advisory == ADVISORY_MODULES, (
        f"advisory modules in the workflow ({sorted(actually_advisory)}) do not match "
        f"the reviewed exception list ({sorted(ADVISORY_MODULES)}); update one or the other deliberately"
    )


def test_harness_step_does_not_swallow_its_own_exit_code() -> None:
    """The `|| echo` fallback made the step succeed no matter what the
    harness returned - independent of continue-on-error. Both had to go
    for the gate to mean anything.
    """
    run = _harness_step().get("run")
    assert isinstance(run, str), "the harness step must be a `run:` step"
    # Comments are stripped: prose *about* the old `|| echo` fallback (in
    # the step's own explanatory comment) is not a use of it.
    code = "\n".join(line for line in run.splitlines() if not line.lstrip().startswith("#"))
    assert "|| echo" not in code, (
        "the harness step swallows its own exit code with `|| echo`, so the "
        "step reports success even when scripts/mutmut_critical.py exits non-zero"
    )
    assert "scripts/mutmut_critical.py" in code


def test_upload_step_still_runs_after_a_failing_harness() -> None:
    """Diagnostics must survive a real gate failure.

    Without `always()`, a harness step that now legitimately fails skips
    the artifact upload that follows it, and the failing module's
    survivor list never leaves the runner.
    """
    condition = _upload_step().get("if", "")
    assert "always()" in str(condition), (
        "Upload module result must run with always() so a failing harness step "
        "still uploads the module's survivor JSON as an artifact"
    )
