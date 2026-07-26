"""Blast-radius scorer + reversibility gate.

Issue #1322. Produces a score in [0, 1] together with a structured rationale
describing which detectors fired and how each contributed. Detectors with
``hard_one_way: true`` force the score to 1.0 (regardless of other inputs)
so schema migrations, secrets writes, ``DROP``/``DELETE`` statements and
``rm -rf`` always surface as one-way doors.

Design notes
------------

The scorer is intentionally additive on top of the existing approval surface
(see issue #1322 non-goals): it produces a number + rationale, and the merge
/ deploy gate is the consumer that decides what to do with it. Default
behaviour for callers that do not opt in via ``max_score`` / the
``--max-blast-radius`` CLI flag is unchanged.

Soft components are normalized by their declared weight so the floor is the
single biggest weight and the ceiling is 1.0 even when many low-severity
detectors fire. A small file-count component is added (capped at 0.2) so a
hundred-file refactor still ranks above a one-line tweak even when none of
the destructive detectors match.

This module is dependency-light on purpose: only the Python stdlib and PyYAML
(already a Bernstein dependency) are required. It does *not* import any
gate-runner state so it can be used from CLI entry points before the
orchestrator is bootstrapped.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

__all__ = [
    "BlastRadiusReport",
    "BlastRadiusScorer",
    "ComponentScore",
    "Detector",
    "DetectorHit",
    "default_detectors_path",
    "load_detectors",
    "score_change",
]


DetectorKind = Literal["content_regex", "path_glob", "path_regex"]


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Detector:
    """A single blast-radius detector loaded from YAML."""

    id: str
    kind: DetectorKind
    pattern: str
    description: str
    severity: str
    weight: float
    hard_one_way: bool = False

    def __post_init__(self) -> None:
        if self.kind not in ("content_regex", "path_glob", "path_regex"):
            raise ValueError(f"Detector {self.id}: unknown kind {self.kind!r}")
        if not 0.0 <= self.weight <= 1.0:
            raise ValueError(f"Detector {self.id}: weight must be in [0, 1], got {self.weight}")


@dataclass(frozen=True, slots=True)
class DetectorHit:
    """A detector that matched, with the evidence that triggered it."""

    detector_id: str
    description: str
    severity: str
    weight: float
    hard_one_way: bool
    matched_paths: tuple[str, ...] = ()
    matched_snippets: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "detector_id": self.detector_id,
            "description": self.description,
            "severity": self.severity,
            "weight": self.weight,
            "hard_one_way": self.hard_one_way,
            "matched_paths": list(self.matched_paths),
            "matched_snippets": list(self.matched_snippets),
        }


@dataclass(frozen=True, slots=True)
class ComponentScore:
    """Contribution of one named component to the total."""

    name: str
    value: float
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "value": round(self.value, 4), "detail": self.detail}


@dataclass(frozen=True, slots=True)
class BlastRadiusReport:
    """Output of :func:`score_change`."""

    score: float
    hard_one_way: bool
    components: tuple[ComponentScore, ...]
    hits: tuple[DetectorHit, ...]
    rationale: str
    files_touched: int
    files: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 4),
            "hard_one_way": self.hard_one_way,
            "components": [c.to_dict() for c in self.components],
            "hits": [h.to_dict() for h in self.hits],
            "rationale": self.rationale,
            "files_touched": self.files_touched,
            "files": list(self.files),
        }

    def exceeds(self, threshold: float) -> bool:
        """Return ``True`` when this change should be refused by the gate.

        A change exceeds the threshold when the numeric score is strictly
        greater than the configured ceiling, or when any ``hard_one_way``
        detector fired and the ceiling is below 1.0. The ``hard_one_way``
        rule preserves the issue-#1322 invariant that schema migrations,
        ``DROP``/``DELETE`` SQL, ``rm -rf`` and secrets writes always
        require explicit approval.
        """
        if self.score > threshold:
            return True
        return bool(self.hard_one_way and threshold < 1.0)


# ---------------------------------------------------------------------------
# Detector loading
# ---------------------------------------------------------------------------


def default_detectors_path() -> Path:
    """Return the on-disk path of the shipped default detector list.

    Search order:

    1. ``<pkg>/_default_templates/blast-radius/detectors.yaml`` -- the
       wheel-bundled copy (see ``[tool.hatch.build.targets.wheel.force-include]``
       in ``pyproject.toml``). This is what installed users see.
    2. ``<repo>/templates/blast-radius/detectors.yaml`` -- the editable
       source-checkout layout used during development.
    3. Walk up from the current file looking for a ``templates/`` sibling.
    """
    # 1. Wheel-bundled copy at ``<pkg>/_default_templates/blast-radius``.
    bundled = Path(__file__).resolve().parents[2] / "_default_templates" / "blast-radius" / "detectors.yaml"
    if bundled.exists():
        return bundled

    # 2. Source-checkout: <repo>/templates/blast-radius/detectors.yaml
    repo_root = Path(__file__).resolve().parents[3]
    candidate = repo_root.parent / "templates" / "blast-radius" / "detectors.yaml"
    if candidate.exists():
        return candidate

    # 3. Fallback: walk up looking for the templates directory.
    for parent in Path(__file__).resolve().parents:
        guess = parent / "templates" / "blast-radius" / "detectors.yaml"
        if guess.exists():
            return guess
    raise FileNotFoundError("templates/blast-radius/detectors.yaml not found on disk")


def load_detectors(path: Path | str | None = None) -> list[Detector]:
    """Load detector definitions from a YAML file.

    When ``path`` is ``None`` the shipped defaults are loaded from
    ``templates/blast-radius/detectors.yaml``.
    """
    import yaml  # type: ignore[import-untyped]  # local import; PyYAML stubs are dev-only

    if path is None:
        path = default_detectors_path()
    text = Path(path).read_text(encoding="utf-8")
    raw = yaml.safe_load(text) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"detectors file {path} must be a mapping at the top level")
    items = raw.get("detectors") or []
    if not isinstance(items, list):
        raise ValueError(f"detectors file {path}: `detectors:` must be a list")

    out: list[Detector] = []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        try:
            detector = Detector(
                id=str(entry["id"]),
                kind=entry["kind"],
                pattern=str(entry["pattern"]),
                description=str(entry.get("description", "")),
                severity=str(entry.get("severity", "medium")),
                weight=float(entry.get("weight", 0.3)),
                hard_one_way=bool(entry.get("hard_one_way", False)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"detectors file {path}: invalid entry {entry!r}: {exc}") from exc
        out.append(detector)
    return out


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


# File-count component is capped so a giant refactor doesn't dominate the
# destructive detector signals. Threshold tuned to mirror the heuristic
# called out in issue #1322 (weight ~0.2 on files_touched).
_FILE_COUNT_CAP = 0.2
_FILE_COUNT_SATURATION = 50  # at >=50 touched files the component is at cap


def _file_count_component(n: int) -> ComponentScore:
    """Return the file-count component score.

    Saturates at ``_FILE_COUNT_CAP`` once the change touches more than
    ``_FILE_COUNT_SATURATION`` files. A pure-doc change with one file gets
    a near-zero contribution here, satisfying the issue-#1322 unit-test
    expectation that doc changes score < 0.1.
    """
    if n <= 0:
        return ComponentScore(name="files_touched", value=0.0, detail="0 files touched")
    fraction = min(1.0, n / _FILE_COUNT_SATURATION)
    value = round(fraction * _FILE_COUNT_CAP, 4)
    return ComponentScore(name="files_touched", value=value, detail=f"{n} file(s) touched")


def _match_path(detector: Detector, path: str) -> bool:
    if detector.kind == "path_glob":
        # Two-step match: ``**/foo`` should match both ``foo`` at the
        # repo root and ``some/dir/foo`` anywhere below it. Python's
        # ``fnmatch`` doesn't expand ``**/`` natively, so we also try
        # the pattern without that prefix.
        pattern = detector.pattern
        if fnmatch.fnmatch(path, pattern):
            return True
        if pattern.startswith("**/"):
            return fnmatch.fnmatch(path, pattern[3:])
        return False
    if detector.kind == "path_regex":
        return re.search(detector.pattern, path) is not None
    return False


def _match_content(detector: Detector, content: str) -> list[str]:
    """Return up to 3 short snippets for content_regex hits, else []."""
    if detector.kind != "content_regex" or not content:
        return []
    flags = 0
    snippets: list[str] = []
    for match in re.finditer(detector.pattern, content, flags=flags):
        # Show the matched line, trimmed.
        start = content.rfind("\n", 0, match.start()) + 1
        end = content.find("\n", match.end())
        if end == -1:
            end = len(content)
        snippet = content[start:end].strip()
        if len(snippet) > 200:
            snippet = snippet[:200] + "..."
        snippets.append(snippet)
        if len(snippets) >= 3:
            break
    return snippets


def _normalize_paths(files: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for path in files:
        p = path.strip()
        if not p or p in seen:
            continue
        seen.add(p)
        ordered.append(p)
    return tuple(ordered)


def _aggregate_soft_components(hits: Sequence[DetectorHit]) -> ComponentScore:
    """Combine non-hard hits into a single ``destructive_signals`` component.

    The contribution scales with the max weight that fired (so one critical
    soft detector dominates), plus a smaller bonus for additional fires so
    a long tail of medium-weight hits still bumps the score.
    """
    soft = [h for h in hits if not h.hard_one_way]
    if not soft:
        return ComponentScore(name="destructive_signals", value=0.0, detail="no destructive detectors fired")
    weights = [h.weight for h in soft]
    primary = max(weights)
    extra = sum(w for w in weights if w < primary) * 0.25  # diminishing returns
    value = min(1.0, primary + extra)
    detail = f"{len(soft)} soft detector(s) fired; max weight {primary:.2f}"
    return ComponentScore(name="destructive_signals", value=value, detail=detail)


def score_change(
    *,
    files: Sequence[str] = (),
    diff_text: str = "",
    file_contents: dict[str, str] | None = None,
    detectors: Sequence[Detector] | None = None,
) -> BlastRadiusReport:
    """Compute a blast-radius report for one set of changes.

    Args:
        files: Paths touched by the change (any path style; matched against
            detector globs / regexes as-is).
        diff_text: Concatenated diff / patch body for content_regex matching.
            Pass an empty string when only path-based signals are available.
        file_contents: Optional mapping from path to full file body. When
            provided, content detectors also scan each file body in
            addition to ``diff_text``.
        detectors: Detector list. ``None`` loads the shipped defaults.

    Returns:
        A :class:`BlastRadiusReport` with score, components and rationale.
    """
    detectors_list = list(detectors) if detectors is not None else load_detectors()
    paths = _normalize_paths(files)

    hits: list[DetectorHit] = []
    for det in detectors_list:
        matched_paths: list[str] = [p for p in paths if _match_path(det, p)]

        snippets: list[str] = []
        if det.kind == "content_regex":
            snippets = _match_content(det, diff_text)
            if file_contents:
                for _path, body in file_contents.items():
                    snippets.extend(_match_content(det, body))
                    if len(snippets) >= 6:
                        break

        if matched_paths or snippets:
            hits.append(
                DetectorHit(
                    detector_id=det.id,
                    description=det.description,
                    severity=det.severity,
                    weight=det.weight,
                    hard_one_way=det.hard_one_way,
                    matched_paths=tuple(matched_paths),
                    matched_snippets=tuple(snippets[:5]),
                )
            )

    hard_one_way = any(h.hard_one_way for h in hits)
    file_component = _file_count_component(len(paths))
    soft_component = _aggregate_soft_components(hits)

    hard_value = 1.0 if hard_one_way else 0.0
    hard_component = ComponentScore(
        name="hard_one_way",
        value=hard_value,
        detail=(
            "hard detector(s) fired: " + ", ".join(h.detector_id for h in hits if h.hard_one_way)
            if hard_one_way
            else "no hard detectors fired"
        ),
    )

    # Combine soft signals and file-count into [0, 1] when no hard
    # detectors fired; otherwise the issue-#1322 invariant forces 1.0.
    score = 1.0 if hard_one_way else min(1.0, soft_component.value + file_component.value)

    components = (hard_component, soft_component, file_component)
    rationale = _build_rationale(score, hard_one_way, hits, file_component)

    return BlastRadiusReport(
        score=round(score, 4),
        hard_one_way=hard_one_way,
        components=components,
        hits=tuple(hits),
        rationale=rationale,
        files_touched=len(paths),
        files=paths,
    )


def _build_rationale(
    score: float,
    hard_one_way: bool,
    hits: Sequence[DetectorHit],
    file_component: ComponentScore,
) -> str:
    parts: list[str] = []
    if hard_one_way:
        ids = ", ".join(h.detector_id for h in hits if h.hard_one_way)
        parts.append(
            f"score=1.0 forced by hard one-way detector(s): {ids}. "
            "Change requires explicit approval before merge / deploy."
        )
    else:
        parts.append(f"score={score:.2f} computed from soft signals + file count.")
        if hits:
            ids = ", ".join(f"{h.detector_id}(w={h.weight:.2f})" for h in hits)
            parts.append(f"Detectors that fired: {ids}.")
        else:
            parts.append("No detectors fired.")
        parts.append(file_component.detail + ".")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Convenience: a class wrapper so consumers can hold an immutable scorer.
# ---------------------------------------------------------------------------


class BlastRadiusScorer:
    """Stateful wrapper around :func:`score_change` for repeated calls."""

    def __init__(self, detectors: Sequence[Detector] | None = None) -> None:
        self._detectors: list[Detector] = list(detectors) if detectors is not None else load_detectors()

    @property
    def detectors(self) -> tuple[Detector, ...]:
        return tuple(self._detectors)

    def score(
        self,
        *,
        files: Sequence[str] = (),
        diff_text: str = "",
        file_contents: dict[str, str] | None = None,
    ) -> BlastRadiusReport:
        return score_change(
            files=files,
            diff_text=diff_text,
            file_contents=file_contents,
            detectors=self._detectors,
        )

    def to_payload(self, report: BlastRadiusReport) -> dict[str, Any]:
        """Render ``report`` as a JSON-safe dict (alias for ``report.to_dict()``)."""
        # Kept as a method so call sites can switch implementations without
        # threading dataclass internals through their code.
        return report.to_dict()


# ---------------------------------------------------------------------------
# Gate integration helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GateDecision:
    """Outcome of evaluating a blast-radius report against an operator ceiling."""

    allowed: bool
    threshold: float
    report: BlastRadiusReport
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "threshold": self.threshold,
            "reason": self.reason,
            "report": self.report.to_dict(),
        }


def evaluate_gate(report: BlastRadiusReport, *, max_score: float | None) -> GateDecision:
    """Decide whether ``report`` clears the operator-supplied ceiling.

    Default behaviour: when ``max_score`` is ``None`` (i.e. the operator did
    not pass ``--max-blast-radius``), the gate is a no-op and ``allowed`` is
    always ``True``. This preserves the issue-#1322 requirement that
    existing runs are unaffected.
    """
    if max_score is None:
        return GateDecision(
            allowed=True,
            threshold=1.0,
            report=report,
            reason="no --max-blast-radius set; gate skipped",
        )
    if not 0.0 <= max_score <= 1.0:
        raise ValueError(f"max_score must be in [0, 1], got {max_score}")
    if report.exceeds(max_score):
        if report.hard_one_way:
            reason = (
                f"refused: hard one-way detector fired, blast-radius score {report.score:.2f} > ceiling {max_score:.2f}"
            )
        else:
            reason = f"refused: blast-radius score {report.score:.2f} > ceiling {max_score:.2f}"
        return GateDecision(allowed=False, threshold=max_score, report=report, reason=reason)
    return GateDecision(
        allowed=True,
        threshold=max_score,
        report=report,
        reason=f"score {report.score:.2f} within ceiling {max_score:.2f}",
    )


# ---------------------------------------------------------------------------
# Persisted report helpers (used by `bernstein blast-radius show <task_id>`).
# ---------------------------------------------------------------------------


_REPORT_DIR = Path(".sdd/metrics/blast_radius")


def report_path_for(task_id: str, *, workdir: Path | None = None) -> Path:
    """Filesystem path where a report for ``task_id`` is persisted."""
    base = Path(workdir) if workdir is not None else Path.cwd()
    return base / _REPORT_DIR / f"{task_id}.json"


def save_report(report: BlastRadiusReport, *, task_id: str, workdir: Path | None = None) -> Path:
    """Persist ``report`` to disk and return the path."""
    import json

    path = report_path_for(task_id, workdir=workdir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = report.to_dict()
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def load_report(task_id: str, *, workdir: Path | None = None) -> BlastRadiusReport | None:
    """Load a previously-saved report; return ``None`` when missing."""
    import json

    path = report_path_for(task_id, workdir=workdir)
    if not path.exists():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    components = tuple(
        ComponentScore(name=c["name"], value=float(c["value"]), detail=str(c.get("detail", "")))
        for c in raw.get("components", [])
    )
    hits = tuple(
        DetectorHit(
            detector_id=str(h["detector_id"]),
            description=str(h.get("description", "")),
            severity=str(h.get("severity", "medium")),
            weight=float(h.get("weight", 0.0)),
            hard_one_way=bool(h.get("hard_one_way", False)),
            matched_paths=tuple(h.get("matched_paths", [])),
            matched_snippets=tuple(h.get("matched_snippets", [])),
        )
        for h in raw.get("hits", [])
    )
    return BlastRadiusReport(
        score=float(raw["score"]),
        hard_one_way=bool(raw["hard_one_way"]),
        components=components,
        hits=hits,
        rationale=str(raw.get("rationale", "")),
        files_touched=int(raw.get("files_touched", 0)),
        files=tuple(raw.get("files", [])),
    )


# ---------------------------------------------------------------------------
# Pre-merge blocking-hook factory (opt-in)
# ---------------------------------------------------------------------------


def make_pre_merge_hook(
    *,
    max_score: float,
    detectors: Sequence[Detector] | None = None,
) -> Any:
    """Return a blocking hook callable suitable for ``pre_merge`` registration.

    The hook reads ``files`` / ``diff_text`` from the
    :class:`BlockingHookPayload`'s ``context`` dict (caller-supplied) and
    returns a deny when the change exceeds ``max_score`` or trips a
    hard one-way detector.

    Wire-in example::

        from bernstein.core.security.blocking_hooks import BlockingHookRunner
        from bernstein.core.quality.blast_radius import make_pre_merge_hook

        runner = BlockingHookRunner()
        runner.register("pre_merge", make_pre_merge_hook(max_score=0.4))

    Default behaviour is unchanged: this is opt-in. The caller decides when
    to register the hook (typically when ``--max-blast-radius`` is set).
    """
    from bernstein.core.security.blocking_hooks import BlockingHookResult

    scorer = BlastRadiusScorer(detectors=detectors)

    def _hook(payload: Any) -> Any:
        context = getattr(payload, "context", {}) or {}
        files = context.get("files") or context.get("changed_files") or ()
        diff_text = context.get("diff_text") or context.get("diff") or ""
        report = scorer.score(files=tuple(files), diff_text=str(diff_text))
        decision = evaluate_gate(report, max_score=max_score)
        if decision.allowed:
            return BlockingHookResult(
                allowed=True,
                reason=decision.reason,
                hook_name="blast_radius",
            )
        return BlockingHookResult(
            allowed=False,
            reason=decision.reason,
            hook_name="blast_radius",
        )

    return _hook


# Re-export ``asdict`` for callers that prefer dataclass utilities directly.
_ = asdict  # silence unused-import (kept available via ``from .blast_radius import asdict``)
