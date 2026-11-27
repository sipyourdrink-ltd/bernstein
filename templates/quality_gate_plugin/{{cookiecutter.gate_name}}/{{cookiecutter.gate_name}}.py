"""{{ cookiecutter.gate_name }} quality gate for Bernstein."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bernstein.core.quality.gate_plugins import GatePlugin
from bernstein.core.quality.gate_runner import GateResult

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.quality.gate_pipeline import GateStatus

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class {{ cookiecutter.gate_class }}Config:
    """Configuration for {{ cookiecutter.gate_name }} gate.

    Attributes:
        enabled: Whether the gate is enabled.
        threshold: Threshold for passing (e.g., max errors allowed).
        fail_on: Severity level that triggers failure.
    """

    enabled: bool = True
    threshold: int = 0
    fail_on: str = "error"  # error, warning, info


class {{ cookiecutter.gate_class }}(GatePlugin):
    """{{ cookiecutter.description }}

    Bernstein discovers this class from ``.bernstein/gates/*.py`` or from the
    ``bernstein.gates`` entry-point group. Add the gate to the pipeline to run
    it::

        quality_gates:
          pipeline:
            - { name: "{{ cookiecutter.gate_name }}", required: true, condition: "any_changed" }

    A ``fail`` result at a required gate blocks the task from being promoted.
    Set ``required: false`` on the pipeline step for advisory mode. This gate
    returns ``blocked=False`` so GateRunner applies the step's blocking policy.
    """

    def __init__(self, config: {{ cookiecutter.gate_class }}Config | None = None) -> None:
        self._config = config or {{ cookiecutter.gate_class }}Config()

    @property
    def name(self) -> str:
        """Unique gate name; it must not collide with a built-in gate."""
        return "{{ cookiecutter.gate_name }}"

    @property
    def required(self) -> bool:
        """Default result metadata; the runner uses the pipeline step setting."""
        return True

    @property
    def condition(self) -> str:
        """Run the gate when any file changed."""
        return "any_changed"

    def scan(self, changed_files: list[str], run_dir: Path) -> tuple[int, int]:
        """Inspect the change and return ``(errors_found, warnings_found)``.

        Replace this placeholder with your gate logic, for example run a
        validation command in ``run_dir`` or read ``changed_files`` and count
        findings by severity.
        """
        return 0, 0

    def run(
        self,
        changed_files: list[str],
        run_dir: Path,
        task_title: str,
        task_description: str,
    ) -> GateResult:
        """Run the {{ cookiecutter.gate_name }} gate and return its verdict."""
        if not self._config.enabled:
            return self._result("skipped", blocked=False, details="Gate disabled in configuration", started=None)

        logger.info("Running {{ cookiecutter.gate_name }} gate for task %r", task_title)
        started = time.monotonic()

        # Let scan errors propagate: GateRunner converts plugin exceptions to
        # inconclusive results with a reason and the pipeline's blocking policy.
        errors_found, warnings_found = self.scan(changed_files, run_dir)

        metadata = {
            "errors": errors_found,
            "warnings": warnings_found,
            "threshold": self._config.threshold,
        }
        if errors_found > self._config.threshold:
            return self._result(
                "fail",
                blocked=False,
                details=f"Found {errors_found} errors (threshold: {self._config.threshold})",
                started=started,
                metadata=metadata,
            )
        if warnings_found > 0 and self._config.fail_on == "warning":
            return self._result(
                "fail",
                blocked=False,
                details=f"Found {warnings_found} warnings",
                started=started,
                metadata=metadata,
            )
        return self._result("pass", blocked=False, details="No issues found", started=started, metadata=metadata)

    def _result(
        self,
        status: GateStatus,
        *,
        blocked: bool,
        details: str,
        started: float | None,
        metadata: dict[str, int] | None = None,
    ) -> GateResult:
        duration_ms = 0 if started is None else int((time.monotonic() - started) * 1000)
        return GateResult(
            name=self.name,
            status=status,
            required=self.required,
            blocked=blocked,
            cached=False,
            duration_ms=duration_ms,
            details=details,
            metadata=dict(metadata or {}),
        )
