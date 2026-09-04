"""Boundary-input probing of the callables a diff changed (issue #3377).

The probe gate is the only verification surface that *executes* the changed
code against inputs the worker did not choose. These tests pin the four
properties that make its verdict usable: the probe set is a pure function of
the changed surface and the seed, a crash-level boundary failure goes red and
names the smallest input that produced it, everything the gate could not probe
is recorded with a reason code, and neither a hang nor a large surface can run
away with the run.
"""

from __future__ import annotations

import asyncio
import shlex
import sys
import textwrap
import time
from pathlib import Path

from bernstein.core.quality.behavior_probe import (
    BehaviorProbeConfig,
    derive_probe_plan,
    probe_changed_surface,
)

_PY = shlex.quote(sys.executable)


def _config(**overrides: object) -> BehaviorProbeConfig:
    """Build a probe config that runs the child on this interpreter."""
    defaults: dict[str, object] = {
        "enabled": True,
        "python_command": _PY,
        "per_callable_timeout_s": 60,
        "gate_timeout_s": 300,
    }
    defaults.update(overrides)
    return BehaviorProbeConfig(**defaults)  # type: ignore[arg-type]


def _write_module(root: Path, source: str, rel: str = "pkg/mod.py") -> str:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    return rel


_CRASHING_SOURCE = """
    def first_item(items: list[int]) -> int:
        \"\"\"Return the first item.\"\"\"
        return items[0]
"""


# ---------------------------------------------------------------------------
# 1-2. Derivation is a pure function of the changed surface and the seed
# ---------------------------------------------------------------------------


def test_probe_set_is_byte_identical_for_the_same_surface_and_seed(tmp_path: Path) -> None:
    rel = _write_module(tmp_path, _CRASHING_SOURCE)
    config = _config()

    first = derive_probe_plan(tmp_path, [rel], config)
    second = derive_probe_plan(tmp_path, [rel], config)

    assert first.callables, "expected the changed public callable to be enumerated"
    assert first.document() == second.document()
    assert first.set_hash == second.set_hash
    assert len(first.set_hash) == 64


def test_probe_set_hash_tracks_the_changed_signature(tmp_path: Path) -> None:
    rel = _write_module(tmp_path, _CRASHING_SOURCE)
    config = _config()
    before = derive_probe_plan(tmp_path, [rel], config).set_hash

    _write_module(
        tmp_path,
        """
        def first_item(items: list[int], offset: int = 0) -> int:
            \"\"\"Return an item.\"\"\"
            return items[offset]
        """,
    )
    after = derive_probe_plan(tmp_path, [rel], config).set_hash

    assert before != after


def test_only_callables_touched_by_the_diff_are_probed(tmp_path: Path) -> None:
    rel = _write_module(
        tmp_path,
        """
        def untouched(value: int) -> int:
            \"\"\"Left alone by this diff.\"\"\"
            return value

        def touched(value: int) -> int:
            \"\"\"Changed by this diff.\"\"\"
            return value
        """,
    )
    config = _config()

    plan = derive_probe_plan(tmp_path, [rel], config, changed_lines={rel: {6, 7}})

    assert [target.name for target in plan.callables] == ["touched"]


# ---------------------------------------------------------------------------
# 3. The load-bearing property: a crash-level boundary failure goes red
# ---------------------------------------------------------------------------


def test_unguarded_index_on_empty_list_goes_red_with_the_minimal_failing_input(tmp_path: Path) -> None:
    rel = _write_module(tmp_path, _CRASHING_SOURCE)

    result = asyncio.run(probe_changed_surface(tmp_path, [rel], _config()))

    assert result.passed is False
    failure = result.receipt.minimal_failing
    assert failure is not None
    assert failure.qualname.endswith("first_item")
    assert failure.exception == "IndexError"
    assert failure.arguments == [[]]
    assert "[]" in result.detail


def test_value_error_on_a_boundary_input_is_tolerated_not_a_crash(tmp_path: Path) -> None:
    rel = _write_module(
        tmp_path,
        """
        def require_name(name: str) -> str:
            \"\"\"Return the trimmed name.

            Raises:
                ValueError: If the name is blank.
            \"\"\"
            trimmed = name.strip()
            if not trimmed:
                raise ValueError("name must not be blank")
            return trimmed
        """,
    )

    result = asyncio.run(probe_changed_surface(tmp_path, [rel], _config()))

    assert result.passed is True
    assert result.receipt.minimal_failing is None
    assert any(outcome.outcome == "tolerated" for outcome in result.receipt.outcomes)


# ---------------------------------------------------------------------------
# 4. Absence is explicit
# ---------------------------------------------------------------------------


def test_callable_without_usable_annotations_is_recorded_unprobed_with_a_reason_code(tmp_path: Path) -> None:
    rel = _write_module(
        tmp_path,
        """
        def bare(value):
            \"\"\"No annotation anywhere.\"\"\"
            return value

        def exotic(value: complex) -> complex:
            \"\"\"An annotation the deriver has no boundary values for.\"\"\"
            return value

        class Holder:
            \"\"\"A class whose methods need a receiver.\"\"\"

            def method(self, value: int) -> int:
                \"\"\"Return the value.\"\"\"
                return value
        """,
    )

    plan = derive_probe_plan(tmp_path, [rel], _config())

    reasons = {entry.qualname: entry.reason for entry in plan.unprobed}
    assert reasons["bare"] == "missing-annotation"
    assert reasons["exotic"] == "unsupported-annotation"
    assert reasons["Holder.method"] == "receiver-required"
    assert plan.callables == ()


# ---------------------------------------------------------------------------
# 5. The receipt replays
# ---------------------------------------------------------------------------


def test_probe_receipt_replays_byte_identically_on_a_rerun_with_the_same_seed(tmp_path: Path) -> None:
    rel = _write_module(tmp_path, _CRASHING_SOURCE)
    config = _config()

    first = asyncio.run(probe_changed_surface(tmp_path, [rel], config))
    second = asyncio.run(probe_changed_surface(tmp_path, [rel], config))

    assert first.receipt.document() == second.receipt.document()
    assert first.receipt.probe_set_hash == second.receipt.probe_set_hash


# ---------------------------------------------------------------------------
# 6-7. The gate cannot hang or explode the run
# ---------------------------------------------------------------------------


def test_hanging_callable_is_bounded_by_the_per_callable_timeout(tmp_path: Path) -> None:
    rel = _write_module(
        tmp_path,
        """
        import time


        def slow(value: int) -> int:
            \"\"\"Never returns within the gate budget.\"\"\"
            time.sleep(120)
            return value
        """,
    )

    started = time.monotonic()
    result = asyncio.run(probe_changed_surface(tmp_path, [rel], _config(per_callable_timeout_s=2)))
    elapsed = time.monotonic() - started

    assert elapsed < 60
    assert result.passed is False
    assert any(outcome.outcome == "timeout" for outcome in result.receipt.outcomes)


def test_probe_counts_are_capped_per_callable_and_per_gate(tmp_path: Path) -> None:
    body = "\n".join(
        f'def fn_{index:02d}(value: int, label: str) -> int:\n    """Probe target."""\n    return value\n\n'
        for index in range(9)
    )
    rel = _write_module(tmp_path, body)

    plan = derive_probe_plan(tmp_path, [rel], _config(max_callables=3, max_probes_per_callable=2))

    assert len(plan.callables) == 3
    assert all(len(target.probes) <= 2 for target in plan.callables)
    assert any(entry.reason == "cap-exceeded" for entry in plan.unprobed)


# ---------------------------------------------------------------------------
# 8-9. Registration and gate wiring
# ---------------------------------------------------------------------------


def test_behavior_probe_is_registered_and_ships_default_off() -> None:
    from bernstein.core.quality.gate_pipeline import (
        _DEFAULT_GATE_SPECS,
        VALID_GATE_NAMES,
        build_default_pipeline,
    )
    from bernstein.core.quality.quality_gates import QualityGatesConfig

    assert "behavior_probe" in VALID_GATE_NAMES
    assert QualityGatesConfig().behavior_probe is False
    assert ("behavior_probe", "behavior_probe", False, "python_changed") in _DEFAULT_GATE_SPECS

    off = [step.name for step in build_default_pipeline(QualityGatesConfig())]
    on = [step.name for step in build_default_pipeline(QualityGatesConfig(behavior_probe=True))]
    assert "behavior_probe" not in off
    assert "behavior_probe" in on


def test_gate_runner_attaches_the_probe_receipt_to_the_gate_result(tmp_path: Path) -> None:
    from bernstein.core.gate_runner import GatePipelineStep, GateRunner
    from bernstein.core.models import Complexity, Scope, Task
    from bernstein.core.quality_gates import QualityGatesConfig

    rel = _write_module(tmp_path, _CRASHING_SOURCE)
    config = QualityGatesConfig(
        pipeline=[GatePipelineStep(name="behavior_probe", required=True, condition="python_changed")],
        cache_enabled=False,
        behavior_probe=True,
        behavior_probe_python_command=_PY,
    )
    runner = GateRunner(config, tmp_path)
    task = Task(
        id="T-probe-1",
        title="Probe the changed surface",
        description="Exercise the behavior probe gate.",
        role="backend",
        scope=Scope.MEDIUM,
        complexity=Complexity.MEDIUM,
        owned_files=[rel],
    )

    report = asyncio.run(runner.run_all(task, tmp_path))

    result = next(item for item in report.results if item.name == "behavior_probe")
    assert result.status == "fail"
    assert result.blocked is True
    receipt = result.metadata["probe_receipt"]
    assert isinstance(receipt, dict)
    assert len(str(receipt["probe_set_hash"])) == 64
    assert receipt["minimal_failing"]["exception"] == "IndexError"
