"""Tier-specific checks over an identified candidate, never the caller's dirty tree.

See ``docs/operations/cross-tier-verification.md`` for the input and trust
contracts. Checks execute local code; the temporary directory is isolation
from working-tree mutations, not a security sandbox for hostile programs.
"""

from __future__ import annotations

import hashlib
import io
import math
import subprocess
import sys
import tarfile
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bernstein.core.verification.contract import Verdict, VerifyContract, VerifyVerdict, verify_evidence_hash

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = ["EdgeVerifier", "ResearchVerifier", "SandboxVerifier", "SearchVerifier"]


def _verdict(
    tier: str,
    result: dict[str, Any],
    evidence: dict[str, Any],
    verdict: Verdict,
    reason: str,
    started: float,
    attempt: int,
) -> VerifyVerdict:
    return VerifyVerdict(
        verdict=verdict,
        tier=tier,
        backend=result.get("backend", "unknown"),
        evidence=evidence,
        evidence_hash=verify_evidence_hash(evidence),
        reason=reason,
        elapsed_s=time.monotonic() - started,
        attempt=attempt,
    )


def _git(workdir: str | Path, *args: str, input: bytes | None = None) -> bytes:
    return subprocess.run(
        ["git", "-C", str(workdir), *args],
        input=input,
        capture_output=True,
        check=True,
        timeout=60,
    ).stdout


@contextmanager
def _candidate(result: dict[str, Any], evidence: dict[str, Any]) -> Iterator[tuple[Path, list[str]]]:
    """Export an exact commit, derive paths from the patch and apply it once."""
    diff = result.get("diff")
    base = result.get("base_commit")
    workdir = result.get("workdir")
    if not isinstance(diff, str) or not diff.strip():
        raise ValueError("no diff in task result")
    if not isinstance(base, str) or len(base) not in (40, 64) or any(c not in "0123456789abcdef" for c in base):
        raise ValueError("base_commit must be a full Git commit hash")
    if not workdir:
        raise ValueError("no workdir in task result")
    if result.get("pre_applied"):
        raise ValueError("pre-applied changes are not accepted; provide a base commit and diff")
    resolved = _git(workdir, "rev-parse", "--verify", base + "^{commit}").decode().strip()
    if resolved != base:
        raise ValueError("base_commit does not identify the exported commit")
    evidence.update(base_commit=base, diff_hash="sha256:" + hashlib.sha256(diff.encode()).hexdigest())
    archive = _git(workdir, "archive", "--format=tar", base)
    with tempfile.TemporaryDirectory(prefix="bernstein-verify-") as directory:
        root = Path(directory)
        with tarfile.open(fileobj=io.BytesIO(archive)) as contents:
            contents.extractall(root, filter="data")
        # git apply works outside a repository, so no shared index or refs change.
        stat = _git(root, "apply", "--numstat", "-z", "-", input=diff.encode())
        paths = [row.split(b"\t", 2)[2].decode() for row in stat.split(b"\0") if row]
        if not paths:
            raise ValueError("diff contains no changed paths")
        for path in paths:
            if Path(path).is_absolute() or ".." in Path(path).parts:
                raise ValueError("diff contains an unsafe path")
        _git(root, "apply", "--check", "-", input=diff.encode())
        _git(root, "apply", "-", input=diff.encode())
        evidence["changed_files"] = paths
        yield root, [p for p in paths if (root / p).is_file()]


@dataclass
class EdgeVerifier(VerifyContract):
    """Apply a patch to ``base_commit`` then lint and test its candidate tree.

    ``changed_files`` from the backend is ignored. ``test_paths`` is supplied
    by the coordinator; otherwise only changed test modules are selected.
    """

    ruff_args: list[str] = field(default_factory=lambda: ["check"])
    pytest_args: list[str] = field(default_factory=lambda: ["-x", "-q", "-p", "no:cacheprovider"])
    timeout_s: int = 120
    test_paths: list[str] | None = None

    @property
    def tier(self) -> str:
        return "edge"

    def verify(self, task_result: dict[str, Any], *, attempt: int = 1) -> VerifyVerdict:
        started = time.monotonic()
        evidence: dict[str, Any] = {"tier": self.tier, "attempt": attempt}

        def finish(verdict: Verdict, reason: str = "") -> VerifyVerdict:
            return _verdict(self.tier, task_result, evidence, verdict, reason, started, attempt)

        try:
            with _candidate(task_result, evidence) as (root, paths):
                evidence["ruff_files"] = [p for p in paths if p.endswith(".py")]
                evidence["ruff_args"] = self.ruff_args
                ok, output = self._run_ruff(paths, str(root))
                evidence.update(ruff_ok=ok, ruff_output=output[:2000])
                if not ok:
                    return finish(Verdict.FAIL, f"ruff check failed: {output[:200]}")
                tests = (
                    self.test_paths
                    if self.test_paths is not None
                    else [
                        p
                        for p in paths
                        if (Path(p).name.startswith("test_") and p.endswith(".py")) or Path(p).name.endswith("_test.py")
                    ]
                )
                evidence["pytest_files"] = tests
                evidence["pytest_args"] = self.pytest_args
                for path in tests:
                    if Path(path).is_absolute() or ".." in Path(path).parts or not (root / path).is_file():
                        return finish(Verdict.FAIL, "test_paths must name files inside the candidate")
                if not tests:
                    evidence["pytest_status"] = "not_selected"
                    return finish(Verdict.PASS, "lint passed; no tests selected")
                result = subprocess.run(
                    [sys.executable, "-m", "pytest", *self.pytest_args, *tests],
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_s,
                    cwd=root,
                )
                evidence.update(
                    pytest_exit_code=result.returncode, pytest_output=(result.stdout + result.stderr)[:2000]
                )
                if result.returncode == 5:
                    evidence["pytest_status"] = "no_tests_collected"
                    return finish(Verdict.FAIL, "pytest collected no tests")
                if result.returncode:
                    return finish(Verdict.RETRY, "pytest failed")
                evidence["pytest_status"] = "passed"
                return finish(Verdict.PASS)
        except subprocess.TimeoutExpired:
            return finish(Verdict.RETRY, "verification timed out")
        except (OSError, ValueError, tarfile.TarError, subprocess.CalledProcessError) as exc:
            evidence["error"] = str(exc)[:2000]
            return finish(Verdict.FAIL, f"candidate preparation or check failed: {exc}")

    def _run_ruff(self, paths: list[str], workdir: str) -> tuple[bool, str]:
        files = [p for p in paths if p.endswith(".py")]
        if not files:
            return True, "no python files to check"
        result = subprocess.run(
            [sys.executable, "-m", "ruff", *self.ruff_args, "--", *files],
            capture_output=True,
            text=True,
            timeout=self.timeout_s,
            cwd=workdir,
        )
        return result.returncode == 0, result.stdout + result.stderr

    def max_retries(self) -> int:
        return 2

    def retry_delay_s(self, attempt: int) -> float:
        return 0.0


@dataclass
class SearchVerifier(VerifyContract):
    """Compare UTF-8 content to a coordinator-supplied trusted manifest.

    Expected digests are lowercase SHA-256 hex keyed by exact URL. They must
    come from an independently retained capture, never from ``task_result``.
    """

    required_keys: frozenset[str] = frozenset({"url", "content"})
    expected_digests: dict[str, str] = field(default_factory=dict)
    manifest_id: str = ""

    @property
    def tier(self) -> str:
        return "search"

    def verify(self, task_result: dict[str, Any], *, attempt: int = 1) -> VerifyVerdict:
        started = time.monotonic()
        evidence: dict[str, Any] = {
            "tier": self.tier,
            "attempt": attempt,
            "manifest_id": self.manifest_id,
            "manifest_hash": verify_evidence_hash(self.expected_digests),
        }

        def finish(verdict: Verdict, reason: str = "") -> VerifyVerdict:
            return _verdict(self.tier, task_result, evidence, verdict, reason, started, attempt)

        results = task_result.get("results")
        if not isinstance(results, list) or not results:
            return finish(Verdict.FAIL, "results missing, empty, or not a list")
        errors = [
            i
            for i, entry in enumerate(results)
            if not isinstance(entry, dict)
            or self.required_keys - entry.keys()
            or not isinstance(entry.get("url"), str)
            or not entry["url"].strip()
        ]
        if errors:
            evidence["schema_errors"] = errors[:10]
            return finish(Verdict.FAIL, "schema validation failed")
        if any(not isinstance(e.get("content"), str) or not e["content"].strip() for e in results):
            return finish(Verdict.RETRY, "empty or non-string content")
        if not self.manifest_id or not self.expected_digests:
            return finish(Verdict.FAIL, "trusted expected digest manifest missing")
        checksums = []
        matched = []
        for entry in results:
            expected = self.expected_digests.get(entry["url"], "")
            if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
                return finish(Verdict.FAIL, "expected digest missing or invalid for result URL")
            digest = hashlib.sha256(entry["content"].encode("utf-8")).hexdigest()
            checksums.append(digest)
            matched.append({"url": entry["url"], "digest": digest})
            if digest != expected:
                evidence["mismatch_url"] = entry["url"]
                evidence["actual_digest"] = digest
                evidence["expected_digest"] = expected
                return finish(Verdict.FAIL, "content checksum mismatch")
        evidence.update(
            result_count=len(results),
            checksum_count=len(checksums),
            content_checksums=checksums,
            matched_results=matched,
        )
        return finish(Verdict.PASS)

    def max_retries(self) -> int:
        return 1

    def retry_delay_s(self, attempt: int) -> float:
        return 2.0


@dataclass
class ResearchVerifier(VerifyContract):
    """Require an identified judge returning a finite score in [0, 1]."""

    threshold: float = 0.7
    judge_fn: Any = None
    judge_id: str = ""

    @property
    def tier(self) -> str:
        return "research"

    def verify(self, task_result: dict[str, Any], *, attempt: int = 1) -> VerifyVerdict:
        started = time.monotonic()
        evidence: dict[str, Any] = {"tier": self.tier, "attempt": attempt, "judge": self.judge_id}

        def finish(verdict: Verdict, reason: str = "") -> VerifyVerdict:
            return _verdict(self.tier, task_result, evidence, verdict, reason, started, attempt)

        report = task_result.get("report")
        if not isinstance(report, str) or not report.strip():
            return finish(Verdict.FAIL, "report missing or empty")
        if not callable(self.judge_fn) or not self.judge_id:
            return finish(Verdict.FAIL, "identified judge required")
        if not math.isfinite(self.threshold) or not 0 <= self.threshold <= 1:
            return finish(Verdict.FAIL, "threshold must be finite and in [0, 1]")
        rubric = task_result.get("rubric", "")
        if not isinstance(rubric, str):
            return finish(Verdict.FAIL, "rubric must be a string")
        evidence.update(
            report_hash="sha256:" + hashlib.sha256(report.encode()).hexdigest(),
            rubric_hash="sha256:" + hashlib.sha256(rubric.encode()).hexdigest(),
            threshold=self.threshold,
        )
        try:
            score = float(self.judge_fn(report, rubric))
        except Exception as exc:
            evidence["judge_error"] = str(exc)[:500]
            return finish(Verdict.RETRY, "judge invocation failed")
        if not math.isfinite(score) or not 0 <= score <= 1:
            evidence["invalid_score"] = repr(score)
            return finish(Verdict.FAIL, "judge score must be finite and in [0, 1]")
        evidence["judge_score"] = score
        if score < self.threshold:
            return finish(Verdict.FAIL, "judge score below threshold")
        return finish(Verdict.PASS)

    def max_retries(self) -> int:
        return 3

    def retry_delay_s(self, attempt: int) -> float:
        return min(2.0**attempt, 30.0)


@dataclass
class SandboxVerifier(VerifyContract):
    """Apply a patch to its base and execute a coordinator-approved command."""

    default_timeout_s: int = 300

    @property
    def tier(self) -> str:
        return "sandbox"

    def verify(self, task_result: dict[str, Any], *, attempt: int = 1) -> VerifyVerdict:
        started = time.monotonic()
        evidence: dict[str, Any] = {"tier": self.tier, "attempt": attempt}

        def finish(verdict: Verdict, reason: str = "") -> VerifyVerdict:
            return _verdict(self.tier, task_result, evidence, verdict, reason, started, attempt)

        command = task_result.get("verify_command")
        if not command:
            return finish(Verdict.FAIL, "no verify_command in task result")
        if not task_result.get("workdir"):
            return finish(Verdict.FAIL, "no workdir in task result")
        if isinstance(command, str):
            command = [command]  # Literal executable name, never shell syntax.
        if not isinstance(command, list) or any(not isinstance(arg, str) or not arg for arg in command):
            return finish(Verdict.FAIL, "verify_command must be an argv list")
        evidence["command_hash"] = verify_evidence_hash({"argv": command})
        try:
            with _candidate(task_result, evidence) as (root, _):
                result = subprocess.run(
                    command, capture_output=True, text=True, timeout=self.default_timeout_s, cwd=root
                )
                evidence.update(exit_code=result.returncode, stdout=result.stdout[:2000], stderr=result.stderr[:2000])
                if result.returncode == 0:
                    return finish(Verdict.PASS)
                return finish(Verdict.RETRY, f"verify command exited {result.returncode}")
        except subprocess.TimeoutExpired:
            return finish(Verdict.RETRY, "verify command timed out")
        except (OSError, ValueError, tarfile.TarError, subprocess.CalledProcessError) as exc:
            evidence["error"] = str(exc)[:2000]
            return finish(Verdict.FAIL, f"candidate preparation or verify command failed: {exc}")

    def max_retries(self) -> int:
        return 2

    def retry_delay_s(self, attempt: int) -> float:
        return float(attempt) * 2.0


TIER_VERIFIERS: dict[str, type[VerifyContract]] = {
    "edge": EdgeVerifier,
    "search": SearchVerifier,
    "research": ResearchVerifier,
    "sandbox": SandboxVerifier,
}


def get_verifier(tier: str, **kwargs: Any) -> VerifyContract:
    """Construct the requested tier; configuration is trusted coordinator input."""
    return TIER_VERIFIERS[tier](**kwargs)
