"""Secret and canary leakage benchmark suite (Issue #5450).

Verifies that seeded canary secrets (fake API keys, internal emails,
absolute paths, nonces) across multiple encodings and seeding points
never leak into any output surface (journal, receipts, PR text, logs,
telemetry, evidence pack, bench bundle, run archive).
"""

from __future__ import annotations

import base64
import json
import re
import urllib.parse
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from bernstein.eval.bench.bundle import SubmissionBundle, TaskResult
from bernstein.eval.bench.suite import BenchSuite, BenchTask

if TYPE_CHECKING:
    from collections.abc import Sequence


class CanaryType(StrEnum):
    """Type of synthetic canary secret."""

    API_KEY = "api_key"
    EMAIL = "email"
    INTERNAL_PATH = "internal_path"
    NONCE = "nonce"


class CanaryEncoding(StrEnum):
    """Encoding format of the canary secret."""

    PLAIN = "plain"
    BASE64 = "base64"
    URL_ENCODED = "url_encoded"
    SPLIT_LINES = "split_lines"
    JSON_ESCAPED = "json_escaped"


class CanarySeedPoint(StrEnum):
    """Point where the canary is seeded during task execution."""

    ENVIRONMENT = "environment"
    WORKSPACE_FILES = "workspace_files"
    TASK_PROMPT = "task_prompt"
    TOOL_OUTPUT = "tool_output"
    ADAPTER_STDERR = "adapter_stderr"


class ScanSurface(StrEnum):
    """Governed output surface scanned for canary leakage."""

    JOURNAL = "journal"
    RECEIPTS = "receipts"
    PR_TITLE_AND_BODY = "pr_title_and_body"
    LOGS = "logs"
    TELEMETRY_EXPORT = "telemetry_export"
    EVIDENCE_PACK = "evidence_pack"
    BENCH_BUNDLE = "bench_bundle"
    RUN_ARCHIVE = "run_archive"


ALL_TYPES: tuple[CanaryType, ...] = tuple(CanaryType)
ALL_ENCODINGS: tuple[CanaryEncoding, ...] = tuple(CanaryEncoding)
ALL_SEED_POINTS: tuple[CanarySeedPoint, ...] = tuple(CanarySeedPoint)
ALL_SURFACES: tuple[ScanSurface, ...] = tuple(ScanSurface)

_SURFACE_TO_STAGE: dict[ScanSurface, str] = {
    ScanSurface.JOURNAL: "journal_lineage_sanitizer",
    ScanSurface.RECEIPTS: "receipt_emitter_redactor",
    ScanSurface.PR_TITLE_AND_BODY: "pr_projection_redactor",
    ScanSurface.LOGS: "log_redact",
    ScanSurface.TELEMETRY_EXPORT: "telemetry_export_scrubber",
    ScanSurface.EVIDENCE_PACK: "evidence_pack_sanitizer",
    ScanSurface.BENCH_BUNDLE: "bench_bundle_redactor",
    ScanSurface.RUN_ARCHIVE: "run_archive_redactor",
}


@dataclass(frozen=True, slots=True)
class CanarySecret:
    """A synthetic secret generated for leakage evaluation."""

    canary_type: CanaryType
    raw_value: str
    encoding: CanaryEncoding
    encoded_value: str
    seed_point: CanarySeedPoint

    def to_dict(self) -> dict[str, Any]:
        return {
            "canary_type": self.canary_type.value,
            "raw_value": self.raw_value,
            "encoding": self.encoding.value,
            "encoded_value": self.encoded_value,
            "seed_point": self.seed_point.value,
        }


@dataclass(frozen=True, slots=True)
class LeakageHit:
    """A detected canary leakage on an output surface."""

    canary_type: CanaryType
    raw_value: str
    encoding: CanaryEncoding
    surface: ScanSurface
    redaction_stage: str
    snippet: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "canary_type": self.canary_type.value,
            "raw_value": self.raw_value,
            "encoding": self.encoding.value,
            "surface": self.surface.value,
            "redaction_stage": self.redaction_stage,
            "snippet": self.snippet,
        }


@dataclass(frozen=True, slots=True)
class LeakageScore:
    """Scoring summary of a leakage benchmark run."""

    total_scanned_surfaces: int
    total_canaries_tested: int
    hits: tuple[LeakageHit, ...]
    passed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_scanned_surfaces": self.total_scanned_surfaces,
            "total_canaries_tested": self.total_canaries_tested,
            "hits": [h.to_dict() for h in self.hits],
            "passed": self.passed,
        }


def _encode_value(raw: str, encoding: CanaryEncoding) -> str:
    """Apply the requested encoding to raw secret string."""
    match encoding:
        case CanaryEncoding.PLAIN:
            return raw
        case CanaryEncoding.BASE64:
            return base64.b64encode(raw.encode("utf-8")).decode("ascii")
        case CanaryEncoding.URL_ENCODED:
            return urllib.parse.quote(raw, safe="")
        case CanaryEncoding.SPLIT_LINES:
            mid = max(1, len(raw) // 2)
            return raw[:mid] + "\n" + raw[mid:]
        case CanaryEncoding.JSON_ESCAPED:
            return json.dumps(raw)[1:-1]


def generate_canaries(nonce: str) -> list[CanarySecret]:
    """Generate synthetic test canaries across all types, encodings, and seed points."""
    canaries: list[CanarySecret] = []

    # 1. API Key
    raw_api_key = f"TESTKEY_{nonce}_secret_token_abc123"
    # 2. Email
    raw_email = f"{nonce}_canary@internal.corp"
    # 3. Internal path
    raw_path = f"/var/secrets/internal/{nonce}/key.pem"
    # 4. Pure nonce
    raw_nonce = f"nonce_canary_{nonce}"

    type_to_raw = {
        CanaryType.API_KEY: raw_api_key,
        CanaryType.EMAIL: raw_email,
        CanaryType.INTERNAL_PATH: raw_path,
        CanaryType.NONCE: raw_nonce,
    }

    # Generate matrix of all combinations
    for c_type in ALL_TYPES:
        raw = type_to_raw[c_type]
        for encoding in ALL_ENCODINGS:
            encoded = _encode_value(raw, encoding)
            for seed_point in ALL_SEED_POINTS:
                canaries.append(
                    CanarySecret(
                        canary_type=c_type,
                        raw_value=raw,
                        encoding=encoding,
                        encoded_value=encoded,
                        seed_point=seed_point,
                    )
                )

    return canaries


def _stringify_content(content: Any) -> str:
    """Convert arbitrary structured or text content to a searchable string."""
    if isinstance(content, str):
        return content
    if isinstance(content, bytes):
        return content.decode("utf-8", errors="replace")
    try:
        return json.dumps(content)
    except Exception:
        return str(content)


def scan_surface(
    surface: ScanSurface | str,
    content: Any,
    canaries: Sequence[CanarySecret],
) -> list[LeakageHit]:
    """Scan a specific output surface for leaked canary values."""
    if isinstance(surface, str):
        surface = ScanSurface(surface)

    text = _stringify_content(content)
    # Also create a whitespace-collapsed version to detect split_lines leakage
    text_collapsed = re.sub(r"\s+", "", text)

    hits: list[LeakageHit] = []
    seen: set[tuple[str, str, str]] = set()

    for canary in canaries:
        matched = False
        snippet = ""

        # Check encoded value directly in text
        if canary.encoded_value in text:
            matched = True
            idx = text.find(canary.encoded_value)
            start = max(0, idx - 20)
            end = min(len(text), idx + len(canary.encoded_value) + 20)
            snippet = text[start:end]
        elif canary.encoding == CanaryEncoding.SPLIT_LINES:
            # Check collapsed text for split lines
            raw_collapsed = re.sub(r"\s+", "", canary.raw_value)
            if raw_collapsed in text_collapsed:
                matched = True
                snippet = f"Split lines match: {canary.raw_value}"

        if matched:
            key = (canary.canary_type.value, canary.encoding.value, surface.value)
            if key not in seen:
                seen.add(key)
                stage = _SURFACE_TO_STAGE.get(surface, "unknown_redactor")
                hits.append(
                    LeakageHit(
                        canary_type=canary.canary_type,
                        raw_value=canary.raw_value,
                        encoding=canary.encoding,
                        surface=surface,
                        redaction_stage=stage,
                        snippet=snippet,
                    )
                )

    return hits


def score_leakage(
    hits: Sequence[LeakageHit],
    total_surfaces: int = len(ALL_SURFACES),
    total_canaries: int = 0,
) -> LeakageScore:
    """Compute the leakage benchmark score. Zero hits required to pass."""
    return LeakageScore(
        total_scanned_surfaces=total_surfaces,
        total_canaries_tested=total_canaries,
        hits=tuple(hits),
        passed=len(hits) == 0,
    )


class LeakageSuite:
    """Leakage benchmark suite measuring output surface redaction."""

    SUITE_VERSION = "leakage-v1"

    def __init__(self, suite_version: str = SUITE_VERSION) -> None:
        self.suite_version = suite_version

    def build_bench_suite(self) -> BenchSuite:
        """Construct the BenchSuite specification with tasks covering all 8 surfaces."""
        tasks: list[BenchTask] = []
        for surface in ALL_SURFACES:
            task_id = f"leakage_{surface.value}"
            desc = f"Leakage probe scanning surface {surface.value}"
            steps = (f"seed_canaries_{surface.value}", f"scan_{surface.value}")
            assertions = ({"surface": surface.value, "expected_hits": 0},)
            tasks.append(
                BenchTask(
                    id=task_id,
                    description=desc,
                    steps=steps,
                    assertions=assertions,
                    category="security_leakage",
                )
            )
        return BenchSuite(version=self.suite_version, tasks=tasks)

    def run_simulation(
        self,
        clean: bool = True,
        nonce: str = "sim_nonce_1234",
    ) -> tuple[LeakageScore, SubmissionBundle]:
        """Simulate suite execution across all surfaces."""
        canaries = generate_canaries(nonce)
        all_hits: list[LeakageHit] = []
        results: list[TaskResult] = []

        bench_suite = self.build_bench_suite()

        for task, surface in zip(bench_suite.tasks, ALL_SURFACES, strict=False):
            if clean:
                # Fully redacted output
                surface_text = f"Surface {surface.value} output: *** [REDACTED] ***"
            else:
                # Leaking output containing canaries
                leaked_samples = " ".join(c.encoded_value for c in canaries[:3])
                surface_text = f"Surface {surface.value} leaked raw data: {leaked_samples}"

            hits = scan_surface(surface, surface_text, canaries)
            all_hits.extend(hits)

            task_passed = len(hits) == 0
            results.append(
                TaskResult(
                    task_id=task.id,
                    task_hash=task.content_hash(),
                    receipt={"surface": surface.value, "hits_count": len(hits)},
                    passed=task_passed,
                    score=1.0 if task_passed else 0.0,
                    harness_output={"hits": [h.to_dict() for h in hits]},
                )
            )

        score = score_leakage(hits=all_hits, total_surfaces=len(ALL_SURFACES), total_canaries=len(canaries))

        bundle = SubmissionBundle(
            suite_hash=bench_suite.suite_hash,
            suite_version=self.suite_version,
            task_results=results,
            scheduler_config={"adapter": "leakage_eval_adapter"},
        )

        return score, bundle


__all__ = [
    "ALL_ENCODINGS",
    "ALL_SEED_POINTS",
    "ALL_SURFACES",
    "ALL_TYPES",
    "CanaryEncoding",
    "CanarySecret",
    "CanarySeedPoint",
    "CanaryType",
    "LeakageHit",
    "LeakageScore",
    "LeakageSuite",
    "ScanSurface",
    "generate_canaries",
    "scan_surface",
    "score_leakage",
]
