"""Compliance control-to-benchmark coverage and latest measurement resolution."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from bernstein.compliance.controls import Control, ControlRegistry
    from bernstein.eval.bench.bundle import SubmissionBundle


@dataclass(frozen=True)
class RecordedMeasurement:
    """Summary of the recorded evaluation metrics from a single benchmark bundle."""

    suite_version: str
    suite_hash: str
    submitted_at: float
    pass_rate: float
    overall_score: float
    passed_tasks: int
    total_tasks: int
    bundle_hash: str = ""

    @classmethod
    def from_bundle(cls, bundle: SubmissionBundle) -> RecordedMeasurement:
        """Build a measurement summary from a verified :class:`SubmissionBundle`."""
        if not math.isfinite(bundle.submitted_at):
            raise ValueError("Benchmark submission time must be finite")
        if not math.isfinite(bundle.overall_score) or any(not math.isfinite(r.score) for r in bundle.task_results):
            raise ValueError("Benchmark scores must be finite")
        total = len(bundle.task_results)
        passed = sum(1 for r in bundle.task_results if r.passed)
        return cls(
            suite_version=bundle.suite_version,
            suite_hash=bundle.suite_hash,
            submitted_at=float(bundle.submitted_at),
            pass_rate=float(bundle.pass_rate),
            overall_score=float(bundle.overall_score),
            passed_tasks=passed,
            total_tasks=total,
            bundle_hash=bundle.bundle_hash(),
        )

    @property
    def display(self) -> str:
        """Human-readable pass-rate and task count summary."""
        return f"{self.pass_rate * 100:.1f}% ({self.passed_tasks}/{self.total_tasks})"

    def sort_key(self) -> tuple[float, str, str, float, float, int, int, str]:
        """Total ordering key so latest-selection is independent of input order."""
        return (
            self.submitted_at,
            self.bundle_hash,
            self.suite_hash,
            self.pass_rate,
            self.overall_score,
            self.total_tasks,
            self.passed_tasks,
            self.suite_version,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialise measurement fields into plain JSON types."""
        return {
            "suite_version": self.suite_version,
            "suite_hash": self.suite_hash,
            "submitted_at": self.submitted_at,
            "pass_rate": self.pass_rate,
            "overall_score": self.overall_score,
            "passed_tasks": self.passed_tasks,
            "total_tasks": self.total_tasks,
            "bundle_hash": self.bundle_hash,
            "display": self.display,
        }


@dataclass(frozen=True)
class ControlCoverageEntry:
    """A compliance control paired with its declaring benchmark suites and measurements."""

    control: Control
    suites: tuple[str, ...]
    suite_measurements: Mapping[str, RecordedMeasurement | None]
    latest_measurement: RecordedMeasurement | None

    @property
    def measurement_display(self) -> str:
        """Human-readable measurement summary, or ``'unavailable'`` when unmeasured."""
        if self.latest_measurement is None:
            return "unavailable"
        if len(self.suites) == 1:
            return self.latest_measurement.display
        return ", ".join(
            f"{suite_name}: {m.display}" if m is not None else f"{suite_name}: unavailable"
            for suite_name, m in self.suite_measurements.items()
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialise the control alongside additive suite coverage and latest measurements."""
        payload = self.control.to_dict()
        payload["suites"] = list(self.suites)
        payload["latest_measurement"] = (
            self.latest_measurement.to_dict() if self.latest_measurement is not None else None
        )
        payload["latest_measurement_display"] = self.measurement_display
        payload["suite_measurements"] = {
            suite_name: (m.to_dict() if m is not None else None) for suite_name, m in self.suite_measurements.items()
        }
        return payload


def select_latest_measurements(
    measurements: Iterable[RecordedMeasurement | SubmissionBundle],
) -> dict[str, RecordedMeasurement]:
    """Select the single latest recorded measurement per ``suite_version``.

    Tie-breaking relies strictly on content fields (:meth:`RecordedMeasurement.sort_key`)
    so the selected measurement never depends on the caller's input order.
    """
    from bernstein.eval.bench.bundle import SubmissionBundle

    latest: dict[str, RecordedMeasurement] = {}
    for item in measurements:
        if isinstance(item, SubmissionBundle) and (
            item.scheduler_config.get("adapter") == "mock" or item.refused_results()
        ):
            continue
        rec = RecordedMeasurement.from_bundle(item) if isinstance(item, SubmissionBundle) else item
        if not math.isfinite(rec.submitted_at):
            raise ValueError("Benchmark submission time must be finite")
        existing = latest.get(rec.suite_version)
        if existing is None or rec.sort_key() > existing.sort_key():
            latest[rec.suite_version] = rec
    return {suite_name: latest[suite_name] for suite_name in sorted(latest)}


def build_control_coverage(
    registry: ControlRegistry,
    suite_controls: Mapping[str, Sequence[str]],
    measurements: Iterable[RecordedMeasurement | SubmissionBundle] = (),
    *,
    framework: str | None = None,
) -> list[ControlCoverageEntry]:
    """Join registry controls with declaring suites and latest recorded measurements."""
    control_to_suites: dict[str, set[str]] = {}
    for suite_name, control_ids in suite_controls.items():
        for control_id in control_ids:
            control_to_suites.setdefault(control_id, set()).add(suite_name)

    latest_by_suite = select_latest_measurements(measurements)
    entries: list[ControlCoverageEntry] = []

    for control in registry.list_controls(framework=framework):
        suites = tuple(sorted(control_to_suites.get(control.control_id, ())))
        per_suite = MappingProxyType({suite_name: latest_by_suite.get(suite_name) for suite_name in suites})
        available = [m for m in per_suite.values() if m is not None]
        latest = max(available, key=lambda m: m.sort_key()) if available else None
        entries.append(
            ControlCoverageEntry(
                control=control,
                suites=suites,
                suite_measurements=per_suite,
                latest_measurement=latest,
            )
        )
    return entries


def discover_bench_coverage_inputs(
    bundle_paths: Sequence[Path] = (),
    bundle_dirs: Sequence[Path] = (),
    *,
    include_default_dirs: bool = True,
    base_dir: Path | None = None,
) -> tuple[dict[str, tuple[str, ...]], list[RecordedMeasurement]]:
    """Collect declaring suites and recorded bundle measurements from local artifacts."""
    from bernstein.eval.bench.bench_cli import _get_suite, builtin_suite_control_map
    from bernstein.eval.bench.bundle import SubmissionBundle
    from bernstein.eval.bench.suite import BenchSuite

    suite_controls: dict[str, tuple[str, ...]] = dict(builtin_suite_control_map())
    manifests: dict[str, BenchSuite] = {}
    bundles = [SubmissionBundle.load(path) for path in bundle_paths]

    dirs_to_scan: list[Path] = list(bundle_dirs)
    if include_default_dirs and not bundle_paths and not bundle_dirs:
        root = base_dir or Path.cwd()
        for rel in (".sdd/bench", ".sdd/benchmarks"):
            candidate = root / rel
            if candidate.is_dir():
                dirs_to_scan.append(candidate)

    for dir_path in dirs_to_scan:
        if not dir_path.is_dir():
            continue
        for json_path in sorted(dir_path.glob("*.json")):
            try:
                raw: object = json.loads(json_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(raw, dict):
                continue
            raw_dict = cast("dict[str, Any]", raw)
            if {"bundle_hash", "suite_version", "task_results"} <= raw_dict.keys():
                bundles.append(SubmissionBundle.from_dict(raw_dict))
            elif {"suite_hash", "version", "tasks", "measurement_control_ids"} <= raw_dict.keys():
                suite = BenchSuite.load(json_path)
                if suite.measurement_control_ids is None:
                    continue
                if suite.version in suite_controls:
                    previous = manifests.get(suite.version) or _get_suite(suite.version)
                    if previous.suite_hash != suite.suite_hash:
                        raise ValueError(f"Conflicting manifests for suite {suite.version!r}")
                suite_controls[suite.version] = suite.measurement_control_ids
                manifests[suite.version] = suite

    measurements = []
    for bundle in bundles:
        name = bundle.suite_version
        if name not in suite_controls or bundle.scheduler_config.get("adapter") == "mock" or bundle.refused_results():
            continue
        if name not in manifests:
            manifests[name] = _get_suite(name)
        suite = manifests[name]
        if bundle.suite_hash != suite.suite_hash or not bundle.task_results:
            continue
        expected = sorted((task.id, task.content_hash()) for task in suite.tasks)
        actual = sorted((result.task_id, result.task_hash) for result in bundle.task_results)
        if actual == expected:
            measurements.append(RecordedMeasurement.from_bundle(bundle))
    return suite_controls, measurements


def render_coverage_text(entries: Sequence[ControlCoverageEntry]) -> str:
    """Render control coverage rows as a fixed-width plain-text table."""
    lines = [
        f"{'Control ID':<14} {'Category':<14} {'Frameworks':<28} {'Suites':<22} {'Latest Measurement':<20} Title",
        "─" * 130,
    ]
    for entry in entries:
        control = entry.control
        fw_list = ", ".join(control.references.keys())
        suites_str = ", ".join(entry.suites) if entry.suites else "-"
        lines.append(
            f"{control.control_id:<14} {control.category:<14} {fw_list:<28} "
            f"{suites_str:<22} {entry.measurement_display:<20} {control.title}"
        )
    lines.append(f"\nTotal: {len(entries)} controls")
    return "\n".join(lines)


def render_coverage_markdown(entries: Sequence[ControlCoverageEntry]) -> str:
    """Render control coverage rows as a Markdown table."""
    headers = [
        "Control ID",
        "Title",
        "Frameworks",
        "Evidence Kinds",
        "Suites",
        "Latest Measurement",
    ]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for entry in entries:
        control = entry.control
        fw_str = ", ".join(k.upper() for k in sorted(control.references.keys()))
        ev_str = ", ".join(control.evidence_kinds)
        suites_str = ", ".join(entry.suites) if entry.suites else "-"
        lines.append(
            "| "
            + " | ".join(
                [
                    control.control_id,
                    control.title,
                    fw_str,
                    ev_str,
                    suites_str,
                    entry.measurement_display,
                ]
            )
            + " |"
        )
    return "\n".join(lines)
