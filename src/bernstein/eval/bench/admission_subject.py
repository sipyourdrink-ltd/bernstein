"""Versioned, signed model/task-class attribution for an evaluation bundle."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.lineage.entry import ModelRef

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

ADMISSION_SUBJECT_VERSION = 1


def task_class_set(values: Sequence[str]) -> tuple[str, ...]:
    """Canonical explicit task classes; no implicit wildcard or empty scope."""
    if isinstance(values, str | bytes) or not values:
        raise ValueError("task_classes must be a non-empty sequence")
    if any(not isinstance(value, str) or not value.strip() or value != value.strip() for value in values):
        raise ValueError("task_classes must contain non-empty, trimmed strings")
    return tuple(sorted(set(values)))


@dataclass(frozen=True)
class AdmissionSubject:
    """What the bundle signer attributes to this evaluation, not a routing hint."""

    model_ref: ModelRef
    task_classes: tuple[str, ...]
    schema_version: int = ADMISSION_SUBJECT_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != ADMISSION_SUBJECT_VERSION:
            raise ValueError("unsupported admission subject schema_version")
        if not isinstance(self.model_ref, ModelRef):
            raise ValueError("admission subject requires a ModelRef")
        self._check_ref(self.model_ref)
        object.__setattr__(self, "task_classes", task_class_set(self.task_classes))

    @staticmethod
    def _check_ref(ref: ModelRef) -> None:
        for name in ("provider", "model_requested", "model_reported", "version"):
            value = getattr(ref, name)
            if value is not None and (not isinstance(value, str) or not value.strip() or value != value.strip()):
                raise ValueError(f"model_ref.{name} must be non-empty, trimmed text")
        if "/" in ref.provider:
            raise ValueError("model_ref.provider must not contain '/'")
        for name in ("provider", "model_requested", "model_reported", "version"):
            value = getattr(ref, name)
            if value is not None and "@" in value:
                raise ValueError(f"model_ref.{name} must not contain the registry delimiter '@'")
        if ref.version == "*":
            raise ValueError("model_ref.version must not be the registry wildcard '*'")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "model_ref": asdict(self.model_ref),
            "task_classes": list(self.task_classes),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> AdmissionSubject:
        if not isinstance(raw, dict) or set(raw) != {"schema_version", "model_ref", "task_classes"}:
            raise ValueError("invalid admission subject fields")
        ref = raw["model_ref"]
        if not isinstance(ref, dict) or set(ref) - {
            "provider",
            "model_requested",
            "model_reported",
            "version",
            "routing_decision_hash",
        }:
            raise ValueError("invalid admission subject model_ref")
        for name, value in ref.items():
            if name == "routing_decision_hash":
                if not isinstance(value, str):
                    raise ValueError("model_ref.routing_decision_hash must be text")
            elif value is not None and not isinstance(value, str):
                raise ValueError(f"model_ref.{name} must be text")
        if not isinstance(raw["task_classes"], list):
            raise ValueError("admission subject task_classes must be a list")
        return cls(
            model_ref=ModelRef(**ref),
            task_classes=tuple(raw["task_classes"]),
            schema_version=raw["schema_version"],
        )
