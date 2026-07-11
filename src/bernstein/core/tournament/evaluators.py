"""Scripted evaluator outputs for a tournament attempt (issue #2353).

Evaluators are *scripted checks* -- their outputs, not a model judgement,
decide the winner. This module owns two shapes and the harness that produces
them:

* :class:`EvaluatorOutput` -- one evaluator's normalised value for one attempt.
* :class:`AttemptOutcome` -- one attempt's content-addressed identity plus the
  evaluator outputs collected against it.

The attempt's identity is the ``sha256`` content hash of its output bytes (the
diff / artefact the attempt produced), so two byte-identical attempts collapse
to the same lineage node and a tampered attempt diverges. Running an evaluator
is separated from *scoring* it: the decision path (:mod:`.scorer`) consumes only
the numeric outputs, never a subprocess or a model, so replay is deterministic.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.lineage.spine import content_hash_of

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EvaluatorOutput:
    """One evaluator's value for one attempt.

    Attributes:
        name: The evaluator name; must match an :class:`EvaluatorSpec` name.
        value: Normalised numeric output. Callers should feed values on a
            comparable scale (a pass rate in ``[0, 1]``, a 0/1 lint status, a
            coverage or mutation fraction); the scorer clamps to ``[0, 1]``.
        detail: Optional human-readable detail retained on the receipt for
            forensics (never part of the numeric decision).
    """

    name: str
    value: float
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "value": self.value, "detail": self.detail}

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> EvaluatorOutput:
        return cls(
            name=str(row["name"]),
            value=float(row["value"]),
            detail=str(row.get("detail", "")),
        )


@dataclass(frozen=True, slots=True)
class AttemptOutcome:
    """One attempt's content-addressed identity plus its evaluator outputs.

    Attributes:
        attempt_hash: ``sha256:`` content hash of the attempt's output bytes.
            This is the attempt's lineage identity.
        outputs: Ordered evaluator outputs collected against the attempt.
    """

    attempt_hash: str
    outputs: tuple[EvaluatorOutput, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_hash": self.attempt_hash,
            "outputs": [o.to_dict() for o in self.outputs],
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> AttemptOutcome:
        return cls(
            attempt_hash=str(row["attempt_hash"]),
            outputs=tuple(EvaluatorOutput.from_dict(o) for o in row.get("outputs", [])),
        )

    @classmethod
    def from_output_bytes(cls, output_bytes: bytes, *, outputs: Sequence[EvaluatorOutput]) -> AttemptOutcome:
        """Build an outcome whose identity is the content hash of ``output_bytes``."""
        return cls(attempt_hash=content_hash_of(output_bytes), outputs=tuple(outputs))

    def output_for(self, name: str) -> EvaluatorOutput | None:
        """Return the output for evaluator ``name`` or ``None`` when absent."""
        for out in self.outputs:
            if out.name == name:
                return out
        return None


def command_status_output(
    *,
    name: str,
    command: Sequence[str],
    cwd: Path,
    timeout: float = 600.0,
) -> EvaluatorOutput:
    """Run a scripted command in ``cwd`` and return a 0/1 status output.

    Reuses the project's verification-harness convention: exit code ``0`` maps
    to ``1.0`` (the check passed), any non-zero exit or failure maps to ``0.0``.
    The command is a fully-constructed argument vector supplied by the caller;
    no shell is used. Only the exit status feeds the numeric output, so the
    decision path stays deterministic given the same command result.
    """
    try:
        completed = subprocess.run(  # nosec B603 - argv supplied by caller, no shell
            list(command),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(cwd),
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("tournament evaluator %s failed to run: %s", name, type(exc).__name__)
        return EvaluatorOutput(name=name, value=0.0, detail=f"error:{type(exc).__name__}")
    value = 1.0 if completed.returncode == 0 else 0.0
    return EvaluatorOutput(name=name, value=value, detail=f"exit:{completed.returncode}")


__all__ = [
    "AttemptOutcome",
    "EvaluatorOutput",
    "command_status_output",
]
