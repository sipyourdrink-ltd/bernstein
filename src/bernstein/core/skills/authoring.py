"""Deterministic authoring helpers for local skill packs."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import yaml

from bernstein.core.skills.lifecycle import SkillDigest, SkillLifecycleError, compute_skill_digest
from bernstein.core.skills.loader import SkillLoader
from bernstein.core.skills.sources.local_dir import LocalDirSkillSource

_TOKEN_RE: re.Pattern[str] = re.compile(r"[a-z0-9][a-z0-9-]*")
_VALID_BUCKETS: tuple[str, ...] = ("references", "scripts", "assets")


class SkillAuthoringError(ValueError):
    """Raised when an authoring suite or skill directory is invalid."""


@dataclass(frozen=True)
class TriggerExpectation:
    """Expected trigger-set membership for one suite case."""

    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()


@dataclass(frozen=True)
class TriggerCase:
    """One deterministic skill trigger-set case."""

    name: str
    query: str
    expect: TriggerExpectation


@dataclass(frozen=True)
class TriggerCaseResult:
    """Result for one trigger-set case."""

    case: TriggerCase
    matched: tuple[str, ...]
    missing: tuple[str, ...]
    unexpected: tuple[str, ...]

    @property
    def passed(self) -> bool:
        """Return whether this case matched its include/exclude expectation."""
        return not self.missing and not self.unexpected


@dataclass(frozen=True)
class TriggerSuiteResult:
    """Result for a full trigger-set suite."""

    cases: tuple[TriggerCaseResult, ...]

    @property
    def passed(self) -> bool:
        """Return whether every suite case passed."""
        return all(case.passed for case in self.cases)

    @property
    def passed_count(self) -> int:
        """Return the number of passing cases."""
        return sum(1 for case in self.cases if case.passed)


@dataclass(frozen=True)
class SkillDiffResult:
    """Structural diff summary for two skill directories."""

    left_digest: SkillDigest
    right_digest: SkillDigest
    changed_sections: tuple[str, ...]

    @property
    def changed(self) -> bool:
        """Return whether the canonical skill digests differ."""
        return self.left_digest.digest != self.right_digest.digest


@dataclass(frozen=True)
class SkillBenchResult:
    """Timing result for repeated deterministic trigger-set runs."""

    suite: TriggerSuiteResult
    iterations: int
    elapsed_seconds: float


def load_trigger_suite(path: Path) -> tuple[TriggerCase, ...]:
    """Load a deterministic trigger-set suite from YAML."""
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SkillAuthoringError(f"{path}: failed to read trigger suite: {exc}") from exc
    if not isinstance(loaded, dict):
        raise SkillAuthoringError(f"{path}: suite must be a YAML mapping")
    raw_cases = cast("dict[str, object]", loaded).get("cases")
    if not isinstance(raw_cases, list):
        raise SkillAuthoringError(f"{path}: suite must contain a cases list")

    cases: list[TriggerCase] = []
    for index, raw_case in enumerate(cast("list[object]", raw_cases), start=1):
        if not isinstance(raw_case, dict):
            raise SkillAuthoringError(f"{path}: case #{index} must be a mapping")
        case_data = cast("dict[str, object]", raw_case)
        name = _required_str(case_data, "name", path=path, index=index)
        query = _required_str(case_data, "query", path=path, index=index)
        expect = _expectation(case_data.get("expect"), path=path, index=index)
        cases.append(TriggerCase(name=name, query=query, expect=expect))
    return tuple(cases)


def run_trigger_suite(skills_root: Path, suite_path: Path) -> TriggerSuiteResult:
    """Run a deterministic trigger-set suite against a local skills root."""
    cases = load_trigger_suite(suite_path)
    results: list[TriggerCaseResult] = []
    for case in cases:
        matched = match_skills(skills_root, case.query)
        matched_set = set(matched)
        missing = tuple(name for name in case.expect.include if name not in matched_set)
        unexpected = tuple(name for name in case.expect.exclude if name in matched_set)
        results.append(
            TriggerCaseResult(
                case=case,
                matched=matched,
                missing=missing,
                unexpected=unexpected,
            )
        )
    return TriggerSuiteResult(cases=tuple(results))


def match_skills(skills_root: Path, query: str) -> tuple[str, ...]:
    """Return skill names whose deterministic trigger hints match ``query``."""
    loader = SkillLoader([LocalDirSkillSource(skills_root, source_name="local-authoring")])
    query_tokens = frozenset(_TOKEN_RE.findall(query.casefold()))
    query_text = " ".join(sorted(query_tokens))
    matches: list[str] = []
    for skill in loader.list_all():
        needles = (skill.name, *skill.trigger_keywords)
        if any(_needle_matches(needle, query_tokens=query_tokens, query_text=query_text) for needle in needles):
            matches.append(skill.name)
    return tuple(matches)


def diff_skill_dirs(left: Path, right: Path) -> SkillDiffResult:
    """Return a canonical structural diff summary for two skill directories."""
    left_dir = _coerce_skill_dir(left)
    right_dir = _coerce_skill_dir(right)
    left_digest = compute_skill_digest(left_dir)
    right_digest = compute_skill_digest(right_dir)
    changed: list[str] = []

    left_front, left_body = _read_skill_parts(left_dir)
    right_front, right_body = _read_skill_parts(right_dir)
    if _canonical_frontmatter(left_front) != _canonical_frontmatter(right_front):
        changed.append("manifest")
    if _normalise_body(left_body) != _normalise_body(right_body):
        changed.append("body")
    if _referenced_file_changes(left_dir, right_dir, left_front, right_front):
        changed.append("files")

    if left_digest.digest != right_digest.digest and not changed:
        changed.append("digest")
    return SkillDiffResult(left_digest=left_digest, right_digest=right_digest, changed_sections=tuple(changed))


def bench_trigger_suite(skills_root: Path, suite_path: Path, *, iterations: int) -> SkillBenchResult:
    """Run the deterministic trigger-set suite repeatedly and time it."""
    if iterations < 1:
        raise SkillAuthoringError("iterations must be at least 1")
    start = time.perf_counter()
    suite = TriggerSuiteResult(cases=())
    for _ in range(iterations):
        suite = run_trigger_suite(skills_root, suite_path)
    return SkillBenchResult(
        suite=suite,
        iterations=iterations,
        elapsed_seconds=time.perf_counter() - start,
    )


def _required_str(data: dict[str, object], key: str, *, path: Path, index: int) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SkillAuthoringError(f"{path}: case #{index} missing string {key!r}")
    return value


def _expectation(raw: object, *, path: Path, index: int) -> TriggerExpectation:
    if raw is None:
        return TriggerExpectation()
    if not isinstance(raw, dict):
        raise SkillAuthoringError(f"{path}: case #{index} expect must be a mapping")
    data = cast("dict[str, object]", raw)
    return TriggerExpectation(
        include=_string_tuple(data.get("include"), path=path, index=index, key="include"),
        exclude=_string_tuple(data.get("exclude"), path=path, index=index, key="exclude"),
    )


def _string_tuple(raw: object, *, path: Path, index: int, key: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise SkillAuthoringError(f"{path}: case #{index} expect.{key} must be a list")
    values: list[str] = []
    for item in cast("list[object]", raw):
        if not isinstance(item, str) or not item:
            raise SkillAuthoringError(f"{path}: case #{index} expect.{key} entries must be strings")
        values.append(item)
    return tuple(values)


def _needle_matches(needle: str, *, query_tokens: frozenset[str], query_text: str) -> bool:
    normalised = needle.casefold().strip()
    if not normalised:
        return False
    if " " in normalised:
        return normalised in query_text
    return normalised in query_tokens


def _coerce_skill_dir(path: Path) -> Path:
    if path.is_dir():
        return path
    if path.name == "SKILL.md" and path.parent.is_dir():
        return path.parent
    raise SkillLifecycleError(f"{path}: expected a skill directory or SKILL.md")


def _read_skill_parts(skill_dir: Path) -> tuple[str, str]:
    skill_md = skill_dir / "SKILL.md"
    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError as exc:
        raise SkillLifecycleError(f"{skill_md}: cannot read SKILL.md: {exc}") from exc
    lines = text.splitlines()
    if not lines or lines[0].rstrip() != "---":
        return ("", text)
    front: list[str] = []
    for index in range(1, len(lines)):
        if lines[index].rstrip() == "---":
            return ("\n".join(front), "\n".join(lines[index + 1 :]).strip("\n"))
        front.append(lines[index])
    return ("", text)


def _canonical_frontmatter(front_raw: str) -> bytes:
    try:
        loaded = yaml.safe_load(front_raw)
    except yaml.YAMLError:
        return front_raw.encode("utf-8")
    if loaded is None:
        loaded = {}
    return yaml.safe_dump(
        loaded,
        sort_keys=True,
        allow_unicode=True,
        default_flow_style=False,
    ).encode("utf-8")


def _normalise_body(body: str) -> bytes:
    return body.replace("\r\n", "\n").encode("utf-8")


def _referenced_file_changes(left: Path, right: Path, left_front: str, right_front: str) -> bool:
    rels = _declared_files(left_front) | _declared_files(right_front)
    for rel in sorted(rels):
        left_file = left / rel
        right_file = right / rel
        if left_file.is_file() != right_file.is_file():
            return True
        if left_file.is_file() and left_file.read_bytes() != right_file.read_bytes():
            return True
    return False


def _declared_files(front_raw: str) -> set[Path]:
    try:
        loaded = yaml.safe_load(front_raw)
    except yaml.YAMLError:
        return set()
    if not isinstance(loaded, dict):
        return set()
    raw = cast("dict[str, Any]", loaded)
    out: set[Path] = set()
    for bucket in _VALID_BUCKETS:
        values = raw.get(bucket, [])
        if not isinstance(values, list):
            continue
        for value in cast("list[object]", values):
            if isinstance(value, str) and value:
                out.add(Path(bucket) / value)
    return out


__all__ = [
    "SkillAuthoringError",
    "SkillBenchResult",
    "SkillDiffResult",
    "TriggerCase",
    "TriggerCaseResult",
    "TriggerExpectation",
    "TriggerSuiteResult",
    "bench_trigger_suite",
    "diff_skill_dirs",
    "load_trigger_suite",
    "match_skills",
    "run_trigger_suite",
]
