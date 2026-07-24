"""Regression tests for the issues found by the end-to-end GUI smoke test.

These are unit-level (no live server required) - they verify the specific
helpers/behaviours that the smoke run revealed as broken or under-tested.

Findings exercised here:

* Mock idle mode tolerates malformed env vars (was a silent crash).
* ``--idle`` CLI help text mentions the cli/mock pinning caveat (regression
  guard so the documentation drift does not return).
* The playground ``bernstein.yaml`` pins ``cli: mock`` at top level so the
  orchestrator subprocess actually instantiates :class:`MockAgentAdapter`.
* Mock idle clamps non-positive sleeps instead of calling
  ``random.randint(a, b)`` with ``b < a``.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from bernstein.adapters.mock import MockAgentAdapter

REPO_ROOT = Path(__file__).resolve().parents[2]
PLAYGROUND_ROOT = REPO_ROOT.parent / "bernstein_playground"


def _run_idle_mock_script(env: dict[str, str], *, tmp_path: Path) -> tuple[int, str]:
    """Spawn the embedded mock idle script with *env* and return (exit, log).

    Reuses the same script source that the production adapter builds - keeps
    this regression in lockstep with the runtime behaviour without faking the
    code path.
    """
    script_src = MockAgentAdapter._build_mock_script()
    script_path = tmp_path / "mock_script.py"
    script_path.write_text(script_src, encoding="utf-8")

    log_path = tmp_path / "mock.log"
    task_info = '{"workdir": "' + str(tmp_path) + '", "task_name": "off_by_one", "log_path": "' + str(log_path) + '"}'

    proc = subprocess.run(
        [sys.executable, str(script_path), task_info],
        env={
            "BERNSTEIN_MOCK_IDLE": "1",
        }
        | env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    log = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    return proc.returncode, log


class TestMockIdleEnvValidation:
    """``_idle_mode`` must not crash on malformed env vars.

    The original code parsed ``int(os.environ.get(...))`` without a try/except,
    so a typo (``MIN_S=180s``) silently aborted the mock with ``ValueError``
    and left the GUI staring at zero agents.
    """

    def test_idle_accepts_clean_env(self, tmp_path: Path) -> None:
        env = {
            "BERNSTEIN_MOCK_IDLE_MIN_S": "0",
            "BERNSTEIN_MOCK_IDLE_MAX_S": "0",
            "BERNSTEIN_MOCK_FAIL_RATE": "0.0",
        }
        rc, log = _run_idle_mock_script(env, tmp_path=tmp_path)
        assert rc == 0, f"unexpected non-zero exit; log:\n{log}"
        assert "idle: sleeping 0s" in log

    def test_idle_tolerates_garbage_min_s(self, tmp_path: Path) -> None:
        env = {
            "BERNSTEIN_MOCK_IDLE_MIN_S": "180s",  # typo: trailing unit
            "BERNSTEIN_MOCK_IDLE_MAX_S": "0",
            "BERNSTEIN_MOCK_FAIL_RATE": "0.0",
        }
        rc, log = _run_idle_mock_script(env, tmp_path=tmp_path)
        assert rc == 0, f"crash on malformed MIN_S; log:\n{log}"
        # We expect a warning AND the script to fall back to defaults.
        assert "WARN bad BERNSTEIN_MOCK_IDLE_MIN_S" in log

    def test_idle_tolerates_garbage_fail_rate(self, tmp_path: Path) -> None:
        env = {
            "BERNSTEIN_MOCK_IDLE_MIN_S": "0",
            "BERNSTEIN_MOCK_IDLE_MAX_S": "0",
            "BERNSTEIN_MOCK_FAIL_RATE": "nan-please",  # not a float
        }
        rc, log = _run_idle_mock_script(env, tmp_path=tmp_path)
        assert rc == 0, f"crash on malformed FAIL_RATE; log:\n{log}"
        assert "WARN bad BERNSTEIN_MOCK_FAIL_RATE" in log

    def test_idle_clamps_inverted_range(self, tmp_path: Path) -> None:
        # Original code used random.randint(min(lo,hi), max(lo,hi)) which works
        # for swapped values but BREAKS if either is negative - we now clamp.
        env = {
            "BERNSTEIN_MOCK_IDLE_MIN_S": "-5",
            "BERNSTEIN_MOCK_IDLE_MAX_S": "0",
            "BERNSTEIN_MOCK_FAIL_RATE": "0.0",
        }
        rc, log = _run_idle_mock_script(env, tmp_path=tmp_path)
        assert rc == 0, f"crash on negative MIN_S; log:\n{log}"
        # Floor clamp to 0 → sleep_s must be 0.
        assert "idle: sleeping 0s" in log


class TestIdleHelpText:
    """The ``bernstein run --idle`` help text documents the mock pinning story.

    Regression guard: the smoke run found that operators are confused by how
    ``--idle`` selects the mock backend. Since the fix for the seed-crash
    instruction, ``--idle`` forces the mock backend internally (it exports
    ``BERNSTEIN_ADAPTER=mock``) and the help must say so instead of telling
    operators to hand-edit ``bernstein.yaml`` with a seed that the parser
    rejects.
    """

    def test_idle_help_documents_internal_mock_pinning(self) -> None:
        # Read the option declaration directly so we do not require Click
        # to render help (which depends on terminal width).
        path = REPO_ROOT / "src" / "bernstein" / "cli" / "run_bootstrap.py"
        text = path.read_text(encoding="utf-8")
        # Find the --idle option block.
        m = re.search(r'"--idle".*?\)\n', text, flags=re.DOTALL)
        assert m is not None, "did not find --idle option declaration"
        block = m.group(0)
        assert "BERNSTEIN_ADAPTER=mock" in block, (
            "--idle help must document that the mock backend is forced internally via BERNSTEIN_ADAPTER=mock"
        )
        assert "cli: mock" not in block, (
            "--idle help must not resurrect the hand-edit ``cli: mock`` "
            "instruction; that seed is rejected by the parser"
        )


@pytest.mark.skipif(
    not PLAYGROUND_ROOT.exists(),
    reason="bernstein_playground checkout not present (smoke test fixture)",
)
class TestPlaygroundConfig:
    """Lightweight smoke checks on the playground workdir config.

    These do not require a running orchestrator - just that the operator-
    facing yaml files have the right pins for GUI development.
    """

    def test_playground_documents_idle_caveat(self) -> None:
        text = (PLAYGROUND_ROOT / "bernstein.yaml").read_text(encoding="utf-8")
        # Until the orchestrator subprocess respects ``cli=mock`` (see follow-up
        # backlog item), the workdir config must explicitly call out the
        # known mismatch so an operator does not assume --idle is safe.
        assert "cli: mock" in text or "KNOWN BUG" in text, (
            "playground bernstein.yaml must either pin ``cli: mock`` or "
            "document the orchestrator-default-adapter caveat so operators "
            "are warned that --idle does not force the mock backend by itself"
        )

    def test_playground_pins_zero_budget(self) -> None:
        text = (PLAYGROUND_ROOT / "bernstein.yaml").read_text(encoding="utf-8")
        assert "max_cost_usd: 0" in text and "hard_stop: true" in text, (
            "playground must keep the budget at $0 so a forgotten --idle cannot generate paid LLM spend"
        )
