"""Secret and canary leakage benchmark suite (Issue #5450).

Canaries are seeded into the inputs a governed run reads, and the bytes the
system actually emits on each output surface are scanned for them. Five
surfaces are driven for real without an orchestrator run -- a bench bundle
saved to disk, an evidence pack exported from a seeded ``.sdd``, a run
archive over a seeded ``.sdd``, a log line through the log sanitizer, and
the PR projection of an evidence bundle. The three that only a governed run
produces (journal, receipts, telemetry export) are reported as *not
exercised*, never as clean.

A hit names the surface, the seed point it was injected at, the canary
type and encoding, and the redaction stage that should have caught it -- or
says that no such stage exists on that path, which is a finding, not a gap
in the suite.

What the suite does *not* cover is reported rather than counted as clean:
three surfaces need a governed run, and so does the ``environment`` seed
point, because none of the five exercised surfaces reads the process
environment. Encodings that produce the same bytes as an earlier one for a
given canary are dropped and listed, so five labels are not mistaken for
five probes.
"""

from __future__ import annotations

import base64
import binascii
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

#: Seed points this suite can actually inject at.
#:
#: ``ENVIRONMENT`` is not one of them. None of the five exercised surfaces
#: reads the process environment -- the archive and the evidence pack zip
#: files off disk, the bundle is assembled in-process, the sanitizer is
#: handed a string -- so a canary placed in ``os.environ`` could only reach
#: an output through an adapter subprocess, which needs a governed run.
#: Declaring it and never injecting it is the failure this constant exists
#: to prevent: the score would have reported four seed points' worth of
#: silence as a clean result for five.
EXERCISED_SEED_POINTS: tuple[CanarySeedPoint, ...] = (
    CanarySeedPoint.WORKSPACE_FILES,
    CanarySeedPoint.TASK_PROMPT,
    CanarySeedPoint.TOOL_OUTPUT,
    CanarySeedPoint.ADAPTER_STDERR,
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
    seed_point: CanarySeedPoint
    surface: ScanSurface
    redaction_stage: str
    snippet: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "canary_type": self.canary_type.value,
            "raw_value": self.raw_value,
            "encoding": self.encoding.value,
            "seed_point": self.seed_point.value,
            "surface": self.surface.value,
            "redaction_stage": self.redaction_stage,
            "snippet": self.snippet,
        }


@dataclass(frozen=True, slots=True)
class LeakageScore:
    """Zero hits on every *exercised* surface passes; what was not exercised is listed, not counted.

    Everything the run did not cover is serialised. A frozen score that
    reported only ``passed`` and a hit list read as a clean sweep of eight
    surfaces and five seed points when it was five and four, and the reader
    of the saved artefact had no way to tell.
    """

    total_scanned_surfaces: int
    total_canaries_tested: int
    hits: tuple[LeakageHit, ...]
    passed: bool
    surfaces_not_exercised: tuple[str, ...] = ()
    seed_points_not_exercised: tuple[str, ...] = ()
    encodings_collapsed: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_scanned_surfaces": self.total_scanned_surfaces,
            "total_canaries_tested": self.total_canaries_tested,
            "hits": [h.to_dict() for h in self.hits],
            "passed": self.passed,
            "surfaces_not_exercised": list(self.surfaces_not_exercised),
            "seed_points_not_exercised": list(self.seed_points_not_exercised),
            "encodings_collapsed": list(self.encodings_collapsed),
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


def _raw_values(nonce: str, seed_point: CanarySeedPoint) -> dict[CanaryType, str]:
    """The four canary shapes for one seed point, all distinct per seed point.

    The seed point is part of every value. Without it the same bytes go in at
    every injection point and a hit cannot say which path it travelled -- and
    "the run archive leaks an API key" is not an actionable finding until it
    says whether the key came from the workspace, the prompt, tool output or
    the adapter's stderr.
    """
    tag = seed_point.value
    return {
        # Shaped like an AWS access key id, the prefix
        # core.security.post_tool_enforcement redacts, so a surface that runs
        # that stage is expected to be clean and one that does not is not.
        CanaryType.API_KEY: "AKIA" + hashlib.sha256(f"{nonce}:{tag}".encode()).hexdigest()[:16].upper(),
        CanaryType.EMAIL: f"{nonce}_{tag}_canary@internal.corp",
        CanaryType.INTERNAL_PATH: f"/var/secrets/internal/{tag}/{nonce}/key.pem",
        CanaryType.NONCE: f"nonce_canary_{tag}_{nonce}",
    }


def _distinct_encodings(raw: str) -> list[tuple[CanaryEncoding, str]]:
    """The encodings that are actually different bytes for *raw*, in declaration order."""
    kept: dict[str, CanaryEncoding] = {}
    out: list[tuple[CanaryEncoding, str]] = []
    for encoding in ALL_ENCODINGS:
        encoded = _encode_value(raw, encoding)
        if encoded in kept:
            continue
        kept[encoded] = encoding
        out.append((encoding, encoded))
    return out


def collapsed_encodings(nonce: str) -> tuple[str, ...]:
    """Encoding labels that are byte-identical to an earlier one, per canary type.

    ``json.dumps`` escapes nothing in any of these four values, and
    ``urllib.parse.quote`` leaves an alphanumeric key id and an
    underscore-separated nonce untouched -- so ``json_escaped`` is the
    plaintext probe under another name for every type, and ``url_encoded``
    is for two of them. Counting them as separate probes inflated both the
    probe count and the hit count without testing anything further. They are
    dropped by :func:`generate_canaries` and named here instead.
    """
    collapses: dict[str, None] = {}
    for seed_point in ALL_SEED_POINTS:
        raws = _raw_values(nonce, seed_point)
        for c_type in ALL_TYPES:
            kept: dict[str, CanaryEncoding] = {}
            for encoding in ALL_ENCODINGS:
                encoded = _encode_value(raws[c_type], encoding)
                if encoded in kept:
                    collapses[f"{c_type.value}: {encoding.value} == {kept[encoded].value}"] = None
                else:
                    kept[encoded] = encoding
    return tuple(collapses)


def generate_canaries(nonce: str) -> list[CanarySecret]:
    """One canary per (seed point, type, *byte-distinct* encoding).

    An encoding is a probe only if it produces different bytes; the labels
    that collapse onto an earlier one are reported by
    :func:`collapsed_encodings` rather than counted twice here.
    """
    canaries: list[CanarySecret] = []
    for seed_point in ALL_SEED_POINTS:
        raws = _raw_values(nonce, seed_point)
        for c_type in ALL_TYPES:
            raw = raws[c_type]
            for encoding, encoded in _distinct_encodings(raw):
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
    # ScanSurface is a StrEnum, so a member is already a str and the
    # constructor is a no-op on one; guarding the call was dead.
    surface = ScanSurface(surface)

    text = _stringify_content(content)
    # Also create a whitespace-collapsed version to detect split_lines leakage
    text_collapsed = re.sub(r"\s+", "", text)

    hits: list[LeakageHit] = []
    seen: set[tuple[str, str, str, str]] = set()

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
        elif canary.encoding == CanaryEncoding.SPLIT_LINES and canary.raw_value not in text:
            # Check collapsed text for split lines
            raw_collapsed = re.sub(r"\s+", "", canary.raw_value)
            if raw_collapsed in text_collapsed:
                matched = True
                snippet = f"Split lines match: {canary.raw_value}"

        if matched:
            key = (canary.canary_type.value, canary.encoding.value, canary.seed_point.value, surface.value)
            if key not in seen:
                seen.add(key)
                stage = _SURFACE_TO_STAGE.get(surface, "unknown_redactor")
                hits.append(
                    LeakageHit(
                        canary_type=canary.canary_type,
                        raw_value=canary.raw_value,
                        encoding=canary.encoding,
                        seed_point=canary.seed_point,
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
    seed_points_not_exercised: Sequence[str] = (),
    encodings_collapsed: Sequence[str] = (),
) -> LeakageScore:
    """Zero hits on the surfaces that were scanned passes; what was not scanned is named."""
    return LeakageScore(
        total_scanned_surfaces=total_surfaces,
        total_canaries_tested=total_canaries,
        hits=tuple(hits),
        passed=len(hits) == 0,
        surfaces_not_exercised=tuple(surfaces_not_exercised),
        seed_points_not_exercised=tuple(seed_points_not_exercised),
        encodings_collapsed=tuple(encodings_collapsed),
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


def _hit_identity(hit: dict[str, Any]) -> tuple[str, str, str, str]:
    """What makes a reported hit the same finding as a rescanned one."""
    return (
        str(hit.get("canary_type", "")),
        str(hit.get("encoding", "")),
        str(hit.get("seed_point", "")),
        str(hit.get("surface", "")),
    )


def build_receipt(
    surface: ScanSurface,
    nonce: str,
    emitted: bytes | None,
    hits: Sequence[LeakageHit],
) -> dict[str, Any]:
    """The replay substrate for one surface.

    ``bench verify`` calls ``score_task`` and never ``run_task``, so anything
    not in here cannot be checked by anyone but the submitter. A receipt that
    carried only ``status`` was therefore its own authority: editing one word
    to ``"clean"`` turned a leaking run into a passing one and the verifier
    had nothing to contradict it with. The bytes the surface emitted and the
    nonce that generates the canaries are both bound in, so
    :meth:`LeakageReplayAdapter.score_task` can re-derive the verdict from
    evidence instead of from a claim.

    The bytes are small by construction -- the largest surface emits a few
    kilobytes -- because every probe is seeded, not harvested from a real run.
    """
    if emitted is None:
        return {
            "journal_head": "",
            "spine_head": "",
            "surface": surface.value,
            "nonce": nonce,
            "status": "not_exercised",
            "stage": _SURFACE_TO_STAGE[surface],
            "hits": [],
            "emitted_sha256": "",
            "emitted_b64": "",
        }
    return {
        "journal_head": "",
        "spine_head": "",
        "surface": surface.value,
        "nonce": nonce,
        "status": "clean" if not hits else "leaked",
        "stage": _SURFACE_TO_STAGE[surface],
        "hits": [h.to_dict() for h in hits],
        "emitted_sha256": hashlib.sha256(emitted).hexdigest(),
        "emitted_b64": base64.b64encode(emitted).decode("ascii"),
    }


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
                receipt=build_receipt(surface, nonce, emitted, hits),
                passed=passed,
                score=1.0 if passed else 0.0,
                harness_output={"hits_count": len(hits), "status": status},
            )
        )

    score = score_leakage(
        hits=all_hits,
        total_surfaces=len(ALL_SURFACES) - len(not_exercised),
        # Only the canaries that were actually injected. Counting the
        # environment ones would report probes that never ran.
        total_canaries=sum(1 for c in canaries if c.seed_point in EXERCISED_SEED_POINTS),
        surfaces_not_exercised=not_exercised,
        seed_points_not_exercised=[s.value for s in ALL_SEED_POINTS if s not in EXERCISED_SEED_POINTS],
        encodings_collapsed=collapsed_encodings(nonce),
    )
    bundle = SubmissionBundle(
        suite_hash=bench_suite.suite_hash,
        suite_version=bench_suite.version,
        task_results=results,
        scheduler_config={"adapter": "leakage_probe"},
    )
    return score, bundle


def score_from_bundle(bundle: SubmissionBundle) -> LeakageScore:
    """Re-derive the suite score from the receipts a bundle carries.

    ``bench run`` goes through the runner and the replay adapter, which
    produce a bundle rather than a :class:`LeakageScore`, so without this the
    coverage the score reports -- which surfaces and seed points were never
    exercised, which encoding labels collapsed -- was computed nowhere a user
    could see it.
    """
    hits: list[LeakageHit] = []
    not_exercised: list[str] = []
    nonce = ""
    for result in bundle.task_results:
        receipt = result.receipt or {}
        nonce = nonce or str(receipt.get("nonce", ""))
        if receipt.get("status") == "not_exercised":
            not_exercised.append(str(receipt.get("surface", "")))
            continue
        for hit in receipt.get("hits") or []:
            hits.append(
                LeakageHit(
                    canary_type=CanaryType(hit["canary_type"]),
                    raw_value=hit["raw_value"],
                    encoding=CanaryEncoding(hit["encoding"]),
                    seed_point=CanarySeedPoint(hit["seed_point"]),
                    surface=ScanSurface(hit["surface"]),
                    redaction_stage=hit.get("redaction_stage", ""),
                    snippet=hit.get("snippet", ""),
                )
            )
    canaries = generate_canaries(nonce) if nonce else []
    return score_leakage(
        hits=hits,
        total_surfaces=len(bundle.task_results) - len(not_exercised),
        total_canaries=sum(1 for c in canaries if c.seed_point in EXERCISED_SEED_POINTS),
        surfaces_not_exercised=not_exercised,
        seed_points_not_exercised=[s.value for s in ALL_SEED_POINTS if s not in EXERCISED_SEED_POINTS],
        encodings_collapsed=collapsed_encodings(nonce) if nonce else (),
    )


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
        hits = [] if emitted is None else scan_surface(surface, emitted, canaries)
        return build_receipt(surface, self._nonce, emitted, hits)

    def score_task(self, task: BenchTask, receipt: dict[str, Any]) -> tuple[bool, float, dict[str, Any]]:
        """Re-derive the verdict by rescanning the bytes the receipt carries.

        The verifier calls this and never ``run_task``, so reading ``status``
        back out made the receipt its own authority. Here the nonce
        regenerates the canaries, the recorded bytes are checked against the
        digest stored with them, and the scan is run again; the verdict comes
        from that scan.

        Raises ``ValueError`` -- which the verifier reports as a divergence --
        when the receipt cannot be independently checked or contradicts
        itself. "I cannot verify this" is a different answer from "this
        failed", and collapsing the two is how a fabricated receipt passes.

        This binds the verdict to the bytes, not the bytes to the run: a
        submitter who emits different bytes is outside what replay can see,
        and the bundle signature is what covers that.
        """
        surface = self._surface_of(receipt)
        nonce = receipt.get("nonce")
        if not isinstance(nonce, str) or not nonce:
            raise ValueError(f"{surface.value}: receipt carries no nonce, so its canaries cannot be regenerated")

        status = receipt.get("status")
        reported = [h for h in (receipt.get("hits") or []) if isinstance(h, dict)]

        if status == "not_exercised":
            if surface in EXERCISED_SURFACES:
                raise ValueError(f"{surface.value}: claimed not exercised, but this suite drives that surface")
            if reported:
                raise ValueError(f"{surface.value}: claimed not exercised while reporting {len(reported)} hit(s)")
            return (False, 0.0, {"surface": surface.value, "status": status, "hits_count": 0})

        if status not in ("clean", "leaked"):
            raise ValueError(f"{surface.value}: receipt status {status!r} is not one of clean/leaked/not_exercised")

        emitted = self._emitted_bytes(surface, receipt)
        rescanned = scan_surface(surface, emitted, generate_canaries(nonce))

        if sorted(map(_hit_identity, (h.to_dict() for h in rescanned))) != sorted(map(_hit_identity, reported)):
            raise ValueError(
                f"{surface.value}: receipt reports {len(reported)} hit(s) but rescanning the bytes it "
                f"carries finds {len(rescanned)}; the receipt contradicts its own evidence"
            )

        passed = not rescanned
        if passed != (status == "clean"):
            raise ValueError(f"{surface.value}: receipt says {status!r} but its own bytes rescan as the opposite")
        return (
            passed,
            1.0 if passed else 0.0,
            {"surface": surface.value, "status": status, "hits_count": len(rescanned)},
        )

    @staticmethod
    def _surface_of(receipt: dict[str, Any]) -> ScanSurface:
        try:
            return ScanSurface(receipt.get("surface", ""))
        except ValueError as exc:
            raise ValueError(f"receipt names no known surface: {receipt.get('surface')!r}") from exc

    @staticmethod
    def _emitted_bytes(surface: ScanSurface, receipt: dict[str, Any]) -> bytes:
        """The bytes the receipt says the surface emitted, checked against their digest."""
        encoded = receipt.get("emitted_b64")
        digest = receipt.get("emitted_sha256")
        if not isinstance(encoded, str) or not encoded or not isinstance(digest, str) or not digest:
            raise ValueError(f"{surface.value}: receipt carries no emitted bytes to rescan")
        try:
            emitted = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"{surface.value}: emitted bytes are not valid base64: {exc}") from exc
        if hashlib.sha256(emitted).hexdigest() != digest:
            raise ValueError(f"{surface.value}: emitted bytes do not match the digest recorded beside them")
        return emitted


__all__ = [
    "ALL_ENCODINGS",
    "ALL_SEED_POINTS",
    "ALL_SURFACES",
    "ALL_TYPES",
    "EXERCISED_SEED_POINTS",
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
    "build_receipt",
    "collapsed_encodings",
    "generate_canaries",
    "probe_surface",
    "run_leakage_probes",
    "scan_surface",
    "score_from_bundle",
    "score_leakage",
]
