"""Secret and canary leakage benchmark suite (Issue #5450).

Canaries are seeded into the inputs a governed run reads, and the bytes the
system actually emits on each output surface are scanned for them. Five
surfaces are driven for real without an orchestrator run -- a bench bundle
saved to disk, an evidence pack exported from a seeded ``.sdd``, a run
archive over a seeded ``.sdd``, a log line through the log sanitizer, and
the PR projection of an evidence bundle. The three that only a governed run
produces (journal, receipts, telemetry export) are reported as *not
exercised*, never as clean.

A hit names the surface, the canary type and encoding, and the redaction
stage that should have caught it -- or says that no such stage exists on
that path, which is a finding, not a gap in the suite.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import tempfile
import urllib.parse
import uuid
import zipfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
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

#: The redaction stage a hit on each surface is attributed to. Where the
#: tree has no redaction on that path the entry says so: a hit there is a
#: finding about the system, and the stage to add is named.
_SURFACE_TO_STAGE: dict[ScanSurface, str] = {
    ScanSurface.JOURNAL: "not exercised: needs a governed run",
    ScanSurface.RECEIPTS: "not exercised: needs a governed run",
    ScanSurface.PR_TITLE_AND_BODY: "github_app.evidence_projection.build_evidence_projection",
    ScanSurface.LOGS: "core.security.sanitize.sanitize_log (escapes control characters; redacts nothing)",
    ScanSurface.TELEMETRY_EXPORT: "not exercised: needs a governed run",
    ScanSurface.EVIDENCE_PACK: "none: compliance.evidence_pack embeds audit, policy and attestation bytes verbatim",
    ScanSurface.BENCH_BUNDLE: "none: eval.bench.bundle writes receipts and harness output verbatim",
    ScanSurface.RUN_ARCHIVE: "none: cli.run_archive zips .sdd files verbatim",
}

#: Surfaces this suite can drive without an orchestrator run.
EXERCISED_SURFACES: tuple[ScanSurface, ...] = (
    ScanSurface.BENCH_BUNDLE,
    ScanSurface.EVIDENCE_PACK,
    ScanSurface.RUN_ARCHIVE,
    ScanSurface.LOGS,
    ScanSurface.PR_TITLE_AND_BODY,
)


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
    """Zero hits on every *exercised* surface passes; unexercised surfaces are listed, not counted."""

    total_scanned_surfaces: int
    total_canaries_tested: int
    hits: tuple[LeakageHit, ...]
    passed: bool
    surfaces_not_exercised: tuple[str, ...] = ()

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

    # 1. API Key -- shaped like an AWS access key id, the prefix
    #    core.security.post_tool_enforcement redacts, so a surface that runs
    #    that stage is expected to be clean and one that does not is not.
    raw_api_key = "AKIA" + hashlib.sha256(nonce.encode("utf-8")).hexdigest()[:16].upper()
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
    surfaces_not_exercised: Sequence[str] = (),
) -> LeakageScore:
    """Zero hits on the surfaces that were scanned passes; unexercised ones are named."""
    return LeakageScore(
        total_scanned_surfaces=total_surfaces,
        total_canaries_tested=total_canaries,
        hits=tuple(hits),
        passed=len(hits) == 0,
        surfaces_not_exercised=tuple(surfaces_not_exercised),
    )


def _seed_text(canaries: Sequence[CanarySecret], seed_point: CanarySeedPoint) -> str:
    """What a run would have read at *seed_point*: every canary, once, in prose."""
    values = [c.encoded_value for c in canaries if c.seed_point is seed_point]
    return "operator note: " + " ".join(values) + "\n"


def _seed_sdd(workdir: Path, canaries: Sequence[CanarySecret]) -> Path:
    """An ``.sdd`` whose audit, policy, attestation and runtime-log inputs carry canaries."""
    sdd = workdir / ".sdd"
    for d in ("audit", "lineage", "metrics", "policy", "attestations", "runtime"):
        (sdd / d).mkdir(parents=True, exist_ok=True)
    tool_output = _seed_text(canaries, CanarySeedPoint.TOOL_OUTPUT)
    event = {
        "timestamp": "2026-01-01T00:00:00Z",
        "event_type": "tool_call",
        "actor": "agent:probe",
        "details": {"output": tool_output},
        "hmac": "probe",
    }
    (sdd / "audit" / "events.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")
    (sdd / "policy" / "policy.yaml").write_text(_seed_text(canaries, CanarySeedPoint.WORKSPACE_FILES), encoding="utf-8")
    (sdd / "attestations" / "operator.txt").write_text(
        _seed_text(canaries, CanarySeedPoint.TASK_PROMPT), encoding="utf-8"
    )
    (sdd / "runtime" / "agent.log").write_text(_seed_text(canaries, CanarySeedPoint.ADAPTER_STDERR), encoding="utf-8")
    return sdd


def _zip_members(path: Path) -> bytes:
    """What a consumer of the zip reads: every member, decompressed, in order."""
    with zipfile.ZipFile(path) as zf:
        return b"".join(zf.read(name) for name in sorted(zf.namelist()))


def probe_surface(surface: ScanSurface, canaries: Sequence[CanarySecret], workdir: Path) -> bytes | None:
    """Drive *surface* for real with seeded inputs and return the bytes it emitted.

    ``None`` means the surface needs a governed run this suite cannot stand
    up on its own; it is reported as not exercised, never as clean.
    """
    if surface is ScanSurface.BENCH_BUNDLE:
        # A task whose tool output and stderr carried the canaries, saved the
        # way `bench run` saves it.
        result = TaskResult(
            task_id="probe",
            task_hash="0" * 64,
            receipt={
                "journal_head": "",
                "spine_head": "",
                "tool_output": _seed_text(canaries, CanarySeedPoint.TOOL_OUTPUT),
            },
            passed=True,
            score=1.0,
            harness_output={"stderr": _seed_text(canaries, CanarySeedPoint.ADAPTER_STDERR)},
        )
        out = workdir / "bundle.json"
        SubmissionBundle(
            suite_hash="0" * 64, suite_version="probe", task_results=[result], scheduler_config={}, submitted_at=0.0
        ).save(out)
        return out.read_bytes()

    if surface is ScanSurface.EVIDENCE_PACK:
        from bernstein.compliance.evidence_pack import build_evidence_pack

        sdd = _seed_sdd(workdir, canaries)
        out = workdir / "evidence.zip"
        build_evidence_pack(sdd_dir=sdd, standard="ai-act", output_path=out)
        return _zip_members(out)

    if surface is ScanSurface.RUN_ARCHIVE:
        from bernstein.cli.run_archive import create_archive

        _seed_sdd(workdir, canaries)
        out = workdir / "archive.zip"
        create_archive(workdir, out)
        return _zip_members(out)

    if surface is ScanSurface.LOGS:
        from bernstein.core.security.sanitize import sanitize_log

        return sanitize_log(_seed_text(canaries, CanarySeedPoint.ADAPTER_STDERR)).encode("utf-8")

    if surface is ScanSurface.PR_TITLE_AND_BODY:
        # A producer whose name and output carried canaries; the projection
        # promises to reference the bundle without embedding evidence bytes.
        from bernstein.core.evidence.bundle import EvidenceBundle, EvidenceItem
        from bernstein.github_app.evidence_projection import build_evidence_projection

        name = _seed_text(canaries, CanarySeedPoint.TASK_PROMPT).strip()
        item = EvidenceItem(
            name=name,
            kind="command",
            required=True,
            status="pass",
            exit_code=0,
            content_hash="sha256:" + "0" * 64,
            size=1,
            truncated=False,
        )
        bundle = EvidenceBundle(task_id="probe-task", items=(item,), gate_passed=True, timestamp=0)
        return build_evidence_projection(bundle).encode("utf-8")

    return None


def run_leakage_probes(nonce: str, workdir: Path) -> tuple[LeakageScore, SubmissionBundle]:
    """Seed canaries, drive every surface this suite can, scan what came out.

    Returns the score and an unsigned bundle with one task per surface; a
    task for a surface that was not exercised records ``status:
    "not_exercised"`` and does not pass, so the pass rate never claims a
    surface that was not looked at.
    """
    canaries = generate_canaries(nonce)
    bench_suite = build_leakage_suite_v1()
    all_hits: list[LeakageHit] = []
    results: list[TaskResult] = []
    not_exercised: list[str] = []

    for task, surface in zip(bench_suite.tasks, ALL_SURFACES, strict=True):
        surface_dir = workdir / surface.value
        surface_dir.mkdir(parents=True, exist_ok=True)
        emitted = probe_surface(surface, canaries, surface_dir)
        if emitted is None:
            not_exercised.append(surface.value)
            hits: list[LeakageHit] = []
            status = "not_exercised"
            passed = False
        else:
            hits = scan_surface(surface, emitted, canaries)
            all_hits.extend(hits)
            status = "clean" if not hits else "leaked"
            passed = not hits
        results.append(
            TaskResult(
                task_id=task.id,
                task_hash=task.content_hash(),
                receipt={
                    "journal_head": "",
                    "spine_head": "",
                    "surface": surface.value,
                    "status": status,
                    "stage": _SURFACE_TO_STAGE[surface],
                    "hits": [h.to_dict() for h in hits],
                },
                passed=passed,
                score=1.0 if passed else 0.0,
                harness_output={"hits_count": len(hits), "status": status},
            )
        )

    score = score_leakage(
        hits=all_hits,
        total_surfaces=len(ALL_SURFACES) - len(not_exercised),
        total_canaries=len(canaries),
        surfaces_not_exercised=not_exercised,
    )
    bundle = SubmissionBundle(
        suite_hash=bench_suite.suite_hash,
        suite_version=bench_suite.version,
        task_results=results,
        scheduler_config={"adapter": "leakage_probe"},
    )
    return score, bundle


def build_leakage_suite_v1() -> BenchSuite:
    """One task per output surface; the suite is fixed, the nonce is per run."""
    tasks: list[BenchTask] = []
    for surface in ALL_SURFACES:
        tasks.append(
            BenchTask(
                id=f"leakage_{surface.value}",
                description=f"Leakage probe scanning surface {surface.value}",
                steps=(f"seed_canaries_{surface.value}", f"scan_{surface.value}"),
                assertions=({"surface": surface.value, "expected_hits": 0},),
                category="security_leakage",
            )
        )
    return BenchSuite(version="leakage-v1", tasks=tasks)


class LeakageReplayAdapter:
    """What `bench run leakage-v1` / `bench verify` go through.

    ``run_task`` drives one surface with a fresh nonce and records the
    canaries found (values included, so the receipt is evidence). Because
    the nonce is per run, two runs do not produce identical receipts by
    design -- a canary that survived one run must not match the next.
    ``score_task`` re-derives the verdict from the receipt alone.
    """

    def __init__(self, nonce: str | None = None) -> None:
        self._nonce = nonce or uuid.uuid4().hex[:12]

    def run_task(self, task: BenchTask, scheduler_config: dict[str, Any]) -> dict[str, Any]:
        surface = ScanSurface(task.id.removeprefix("leakage_"))
        canaries = generate_canaries(self._nonce)
        with tempfile.TemporaryDirectory(prefix="leakage-") as tmp:
            emitted = probe_surface(surface, canaries, Path(tmp))
        if emitted is None:
            return {
                "journal_head": "",
                "spine_head": "",
                "surface": surface.value,
                "status": "not_exercised",
                "stage": _SURFACE_TO_STAGE[surface],
                "hits": [],
            }
        hits = scan_surface(surface, emitted, canaries)
        return {
            "journal_head": "",
            "spine_head": "",
            "surface": surface.value,
            "status": "clean" if not hits else "leaked",
            "stage": _SURFACE_TO_STAGE[surface],
            "hits": [h.to_dict() for h in hits],
        }

    def score_task(self, task: BenchTask, receipt: dict[str, Any]) -> tuple[bool, float, dict[str, Any]]:
        status = receipt.get("status")
        passed = status == "clean" and not receipt.get("hits")
        return (
            passed,
            1.0 if passed else 0.0,
            {"surface": receipt.get("surface"), "status": status, "hits_count": len(receipt.get("hits") or [])},
        )


__all__ = [
    "ALL_ENCODINGS",
    "ALL_SEED_POINTS",
    "ALL_SURFACES",
    "ALL_TYPES",
    "EXERCISED_SURFACES",
    "CanaryEncoding",
    "CanarySecret",
    "CanarySeedPoint",
    "CanaryType",
    "LeakageHit",
    "LeakageReplayAdapter",
    "LeakageScore",
    "ScanSurface",
    "build_leakage_suite_v1",
    "generate_canaries",
    "probe_surface",
    "run_leakage_probes",
    "scan_surface",
    "score_leakage",
]
