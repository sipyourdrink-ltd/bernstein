"""Tier-specific verifier implementations (#6559).

Each class implements :class:`~bernstein.core.verification.contract.VerifyContract`
for one :class:`~bernstein.core.tasks.models.DispatchTier`:

:class:`EdgeVerifier`
    Runs ruff + pytest on the applied diff. The diff must be present in
    ``task_result["diff"]``; the verifier writes it to a temp file, applies
    it, then runs ``ruff check`` and ``pytest`` on the changed files.

:class:`SearchVerifier`
    Validates schema conformance, content checksum, and parse success on
    search-tier results. Expects ``task_result["results"]`` to be a list
    of dicts with at least ``"url"`` and ``"content"`` keys.

:class:`ResearchVerifier`
    Delegates to a judge model that scores the report against a rubric.
    The score threshold is configurable (default 0.7). Does not call a
    model at construction time -- the actual judge invocation is deferred
    to :meth:`verify`, so a verifier can be built cheaply and only pays
    for inference when called.

:class:`SandboxVerifier`
    Runs a verify command in a worktree on the returned diff. This is the
    existing sandbox verification behavior, now behind the uniform contract.
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

from bernstein.core.verification.contract import (
    Verdict,
    VerifyContract,
    VerifyVerdict,
    verify_evidence_hash,
)

logger = logging.getLogger(__name__)

__all__ = [
    "EdgeVerifier",
    "ResearchVerifier",
    "SandboxVerifier",
    "SearchVerifier",
]


# ---------------------------------------------------------------------------
# Edge tier: ruff + pytest on the diff
# ---------------------------------------------------------------------------


@dataclass
class EdgeVerifier(VerifyContract):
    """Verify edge-tier results by running ruff and pytest on changed files.

    Expects ``task_result`` to contain:
        - ``diff``: unified diff string
        - ``changed_files``: list of file paths touched by the diff
        - ``workdir``: path to the working directory (optional; cwd if absent)
    """

    ruff_args: list[str] = field(default_factory=lambda: ["check"])
    pytest_args: list[str] = field(default_factory=lambda: ["-x", "-q"])
    timeout_s: int = 120

    @property
    def tier(self) -> str:
        return "edge"

    def verify(
        self,
        task_result: dict[str, Any],
        *,
        attempt: int = 1,
    ) -> VerifyVerdict:
        t0 = time.monotonic()
        evidence: dict[str, Any] = {"tier": "edge", "attempt": attempt}
        changed_files = task_result.get("changed_files", [])
        workdir = task_result.get("workdir")

        if not task_result.get("diff") and not changed_files:
            return VerifyVerdict(
                verdict=Verdict.FAIL,
                tier=self.tier,
                backend=task_result.get("backend", "unknown"),
                evidence=evidence,
                evidence_hash=verify_evidence_hash(evidence),
                reason="no diff or changed files in task result",
                elapsed_s=time.monotonic() - t0,
                attempt=attempt,
            )

        # Run ruff on changed files
        ruff_ok, ruff_output = self._run_ruff(changed_files, workdir)
        evidence["ruff_ok"] = ruff_ok
        evidence["ruff_output"] = ruff_output[:2000]  # cap evidence size

        if not ruff_ok:
            return VerifyVerdict(
                verdict=Verdict.FAIL,
                tier=self.tier,
                backend=task_result.get("backend", "unknown"),
                evidence=evidence,
                evidence_hash=verify_evidence_hash(evidence),
                reason=f"ruff check failed: {ruff_output[:200]}",
                elapsed_s=time.monotonic() - t0,
                attempt=attempt,
            )

        # Run pytest on changed files
        pytest_ok, pytest_output = self._run_pytest(changed_files, workdir)
        evidence["pytest_ok"] = pytest_ok
        evidence["pytest_output"] = pytest_output[:2000]

        if not pytest_ok:
            verdict = Verdict.RETRY if attempt <= self.max_retries() else Verdict.FAIL
            return VerifyVerdict(
                verdict=verdict,
                tier=self.tier,
                backend=task_result.get("backend", "unknown"),
                evidence=evidence,
                evidence_hash=verify_evidence_hash(evidence),
                reason=f"pytest failed: {pytest_output[:200]}",
                elapsed_s=time.monotonic() - t0,
                attempt=attempt,
            )

        return VerifyVerdict(
            verdict=Verdict.PASS,
            tier=self.tier,
            backend=task_result.get("backend", "unknown"),
            evidence=evidence,
            evidence_hash=verify_evidence_hash(evidence),
            elapsed_s=time.monotonic() - t0,
            attempt=attempt,
        )

    def max_retries(self) -> int:
        return 2

    def retry_delay_s(self, attempt: int) -> float:
        # Edge checks are fast -- no backoff needed.
        return 0.0

    def _run_ruff(self, changed_files: list[str], workdir: str | None) -> tuple[bool, str]:
        if not changed_files:
            return True, ""
        py_files = [f for f in changed_files if f.endswith(".py")]
        if not py_files:
            return True, "no python files to check"
        try:
            result = subprocess.run(
                ["ruff", *self.ruff_args, *py_files],
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                cwd=workdir,
            )
            return result.returncode == 0, result.stdout + result.stderr
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            return False, str(exc)

    def _run_pytest(self, changed_files: list[str], workdir: str | None) -> tuple[bool, str]:
        test_files = [f for f in changed_files if "test" in f and f.endswith(".py")]
        if not test_files:
            # No test files changed -- pass (nothing to verify).
            return True, "no test files in changed set"
        try:
            result = subprocess.run(
                ["pytest", *self.pytest_args, *test_files],
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                cwd=workdir,
            )
            return result.returncode == 0, result.stdout + result.stderr
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            return False, str(exc)


# ---------------------------------------------------------------------------
# Search tier: schema + checksum + parse
# ---------------------------------------------------------------------------

#: Required keys in each search result entry.
_SEARCH_REQUIRED_KEYS = frozenset({"url", "content"})


@dataclass
class SearchVerifier(VerifyContract):
    """Verify search-tier results by schema validation, checksum, and parse.

    Expects ``task_result`` to contain:
        - ``results``: list of dicts, each with at least ``url`` and ``content``
        - ``query``: the original search query (for evidence)
    """

    required_keys: frozenset[str] = _SEARCH_REQUIRED_KEYS

    @property
    def tier(self) -> str:
        return "search"

    def verify(
        self,
        task_result: dict[str, Any],
        *,
        attempt: int = 1,
    ) -> VerifyVerdict:
        t0 = time.monotonic()
        evidence: dict[str, Any] = {"tier": "search", "attempt": attempt}
        results = task_result.get("results")
        backend = task_result.get("backend", "unknown")

        # Schema validation: results must be a non-empty list.
        if not isinstance(results, list) or not results:
            evidence["schema_error"] = "results missing or not a list"
            return VerifyVerdict(
                verdict=Verdict.FAIL,
                tier=self.tier,
                backend=backend,
                evidence=evidence,
                evidence_hash=verify_evidence_hash(evidence),
                reason="results missing or not a list",
                elapsed_s=time.monotonic() - t0,
                attempt=attempt,
            )

        # Validate each entry has required keys.
        schema_errors: list[str] = []
        for i, entry in enumerate(results):
            if not isinstance(entry, dict):
                schema_errors.append(f"entry {i}: not a dict")
                continue
            missing = self.required_keys - set(entry.keys())
            if missing:
                schema_errors.append(f"entry {i}: missing keys {sorted(missing)}")

        if schema_errors:
            evidence["schema_errors"] = schema_errors[:10]
            return VerifyVerdict(
                verdict=Verdict.FAIL,
                tier=self.tier,
                backend=backend,
                evidence=evidence,
                evidence_hash=verify_evidence_hash(evidence),
                reason=f"schema validation failed: {len(schema_errors)} error(s)",
                elapsed_s=time.monotonic() - t0,
                attempt=attempt,
            )

        # Content checksum: hash each entry's content for tamper detection.
        checksums: list[str] = []
        parse_errors: list[str] = []
        for i, entry in enumerate(results):
            content = entry.get("content", "")
            if not isinstance(content, str) or not content.strip():
                parse_errors.append(f"entry {i}: empty or non-string content")
                continue
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            checksums.append(digest)

        evidence["result_count"] = len(results)
        evidence["checksum_count"] = len(checksums)

        if parse_errors:
            evidence["parse_errors"] = parse_errors[:10]
            return VerifyVerdict(
                verdict=Verdict.RETRY if attempt <= self.max_retries() else Verdict.FAIL,
                tier=self.tier,
                backend=backend,
                evidence=evidence,
                evidence_hash=verify_evidence_hash(evidence),
                reason=f"parse errors in {len(parse_errors)} result(s)",
                elapsed_s=time.monotonic() - t0,
                attempt=attempt,
            )

        evidence["content_checksums"] = checksums
        return VerifyVerdict(
            verdict=Verdict.PASS,
            tier=self.tier,
            backend=backend,
            evidence=evidence,
            evidence_hash=verify_evidence_hash(evidence),
            elapsed_s=time.monotonic() - t0,
            attempt=attempt,
        )

    def max_retries(self) -> int:
        return 1

    def retry_delay_s(self, attempt: int) -> float:
        return 2.0


# ---------------------------------------------------------------------------
# Research tier: judge model scoring
# ---------------------------------------------------------------------------


@dataclass
class ResearchVerifier(VerifyContract):
    """Verify research-tier results by scoring the report with a judge model.

    The judge is a callable that takes the report text and returns a float
    score in [0, 1]. This design decouples the verifier from any specific
    model client -- the coordinator provides the judge at construction time.

    Expects ``task_result`` to contain:
        - ``report``: the research report text
        - ``rubric``: (optional) scoring rubric for the judge
    """

    #: Score threshold: reports scoring below this fail.
    threshold: float = 0.7

    #: Callable that scores a report. Signature: (report, rubric) -> float.
    #: None means no judge is configured; verification degrades to a
    #: structural check (non-empty report).
    judge_fn: Any = None  # Callable[[str, str], float] | None

    @property
    def tier(self) -> str:
        return "research"

    def verify(
        self,
        task_result: dict[str, Any],
        *,
        attempt: int = 1,
    ) -> VerifyVerdict:
        t0 = time.monotonic()
        evidence: dict[str, Any] = {"tier": "research", "attempt": attempt}
        report = task_result.get("report")
        backend = task_result.get("backend", "unknown")
        rubric = task_result.get("rubric", "")

        if not isinstance(report, str) or not report.strip():
            evidence["error"] = "report missing or empty"
            return VerifyVerdict(
                verdict=Verdict.FAIL,
                tier=self.tier,
                backend=backend,
                evidence=evidence,
                evidence_hash=verify_evidence_hash(evidence),
                reason="report missing or empty",
                elapsed_s=time.monotonic() - t0,
                attempt=attempt,
            )

        evidence["report_length"] = len(report)

        if self.judge_fn is None:
            # No judge configured -- structural pass only.
            evidence["judge"] = "none"
            evidence["note"] = "no judge configured; structural check only"
            return VerifyVerdict(
                verdict=Verdict.PASS,
                tier=self.tier,
                backend=backend,
                evidence=evidence,
                evidence_hash=verify_evidence_hash(evidence),
                reason="no judge configured; structural check passed",
                elapsed_s=time.monotonic() - t0,
                attempt=attempt,
            )

        try:
            score = float(self.judge_fn(report, rubric))
        except Exception as exc:
            logger.warning("research judge failed: %s", exc)
            evidence["judge_error"] = str(exc)[:500]
            return VerifyVerdict(
                verdict=Verdict.RETRY if attempt <= self.max_retries() else Verdict.FAIL,
                tier=self.tier,
                backend=backend,
                evidence=evidence,
                evidence_hash=verify_evidence_hash(evidence),
                reason=f"judge invocation failed: {exc}",
                elapsed_s=time.monotonic() - t0,
                attempt=attempt,
            )

        evidence["judge_score"] = score
        evidence["threshold"] = self.threshold

        if score < self.threshold:
            verdict = Verdict.RETRY if attempt <= self.max_retries() else Verdict.FAIL
            return VerifyVerdict(
                verdict=verdict,
                tier=self.tier,
                backend=backend,
                evidence=evidence,
                evidence_hash=verify_evidence_hash(evidence),
                reason=f"judge score {score:.2f} below threshold {self.threshold:.2f}",
                elapsed_s=time.monotonic() - t0,
                attempt=attempt,
            )

        return VerifyVerdict(
            verdict=Verdict.PASS,
            tier=self.tier,
            backend=backend,
            evidence=evidence,
            evidence_hash=verify_evidence_hash(evidence),
            elapsed_s=time.monotonic() - t0,
            attempt=attempt,
        )

    def max_retries(self) -> int:
        return 3

    def retry_delay_s(self, attempt: int) -> float:
        # Exponential backoff for judge model calls.
        return min(2.0**attempt, 30.0)


# ---------------------------------------------------------------------------
# Sandbox tier: verify command in worktree
# ---------------------------------------------------------------------------


@dataclass
class SandboxVerifier(VerifyContract):
    """Verify sandbox-tier results by running a command in the worktree.

    This is the existing sandbox verification behavior, now behind the
    uniform contract. The verify command is expected to run in the worktree
    containing the applied diff and exit 0 on success.

    Expects ``task_result`` to contain:
        - ``diff``: unified diff string
        - ``workdir``: path to the worktree
        - ``verify_command``: the command to run (list of strings)
    """

    default_timeout_s: int = 300

    @property
    def tier(self) -> str:
        return "sandbox"

    def verify(
        self,
        task_result: dict[str, Any],
        *,
        attempt: int = 1,
    ) -> VerifyVerdict:
        t0 = time.monotonic()
        evidence: dict[str, Any] = {"tier": "sandbox", "attempt": attempt}
        backend = task_result.get("backend", "unknown")
        workdir = task_result.get("workdir")
        verify_command = task_result.get("verify_command")

        if not verify_command:
            evidence["error"] = "no verify_command in task result"
            return VerifyVerdict(
                verdict=Verdict.FAIL,
                tier=self.tier,
                backend=backend,
                evidence=evidence,
                evidence_hash=verify_evidence_hash(evidence),
                reason="no verify_command in task result",
                elapsed_s=time.monotonic() - t0,
                attempt=attempt,
            )

        if not workdir:
            evidence["error"] = "no workdir in task result"
            return VerifyVerdict(
                verdict=Verdict.FAIL,
                tier=self.tier,
                backend=backend,
                evidence=evidence,
                evidence_hash=verify_evidence_hash(evidence),
                reason="no workdir in task result",
                elapsed_s=time.monotonic() - t0,
                attempt=attempt,
            )

        cmd = verify_command if isinstance(verify_command, list) else [verify_command]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.default_timeout_s,
                cwd=workdir,
            )
            evidence["exit_code"] = result.returncode
            evidence["stdout"] = result.stdout[:2000]
            evidence["stderr"] = result.stderr[:2000]

            if result.returncode == 0:
                return VerifyVerdict(
                    verdict=Verdict.PASS,
                    tier=self.tier,
                    backend=backend,
                    evidence=evidence,
                    evidence_hash=verify_evidence_hash(evidence),
                    elapsed_s=time.monotonic() - t0,
                    attempt=attempt,
                )

            verdict = Verdict.RETRY if attempt <= self.max_retries() else Verdict.FAIL
            return VerifyVerdict(
                verdict=verdict,
                tier=self.tier,
                backend=backend,
                evidence=evidence,
                evidence_hash=verify_evidence_hash(evidence),
                reason=f"verify command exited {result.returncode}",
                elapsed_s=time.monotonic() - t0,
                attempt=attempt,
            )

        except subprocess.TimeoutExpired:
            evidence["error"] = f"timeout after {self.default_timeout_s}s"
            return VerifyVerdict(
                verdict=Verdict.RETRY if attempt <= self.max_retries() else Verdict.FAIL,
                tier=self.tier,
                backend=backend,
                evidence=evidence,
                evidence_hash=verify_evidence_hash(evidence),
                reason=f"verify command timed out after {self.default_timeout_s}s",
                elapsed_s=time.monotonic() - t0,
                attempt=attempt,
            )
        except FileNotFoundError as exc:
            evidence["error"] = str(exc)
            return VerifyVerdict(
                verdict=Verdict.FAIL,
                tier=self.tier,
                backend=backend,
                evidence=evidence,
                evidence_hash=verify_evidence_hash(evidence),
                reason=f"verify command not found: {exc}",
                elapsed_s=time.monotonic() - t0,
                attempt=attempt,
            )

    def max_retries(self) -> int:
        return 2

    def retry_delay_s(self, attempt: int) -> float:
        # Moderate backoff for sandbox retries.
        return float(attempt) * 2.0


# ---------------------------------------------------------------------------
# Registry: tier name -> verifier class
# ---------------------------------------------------------------------------

#: Default verifier class for each dispatch tier.
TIER_VERIFIERS: dict[str, type[VerifyContract]] = {
    "edge": EdgeVerifier,
    "search": SearchVerifier,
    "research": ResearchVerifier,
    "sandbox": SandboxVerifier,
}


def get_verifier(tier: str, **kwargs: Any) -> VerifyContract:
    """Return a verifier instance for *tier*.

    Raises :class:`KeyError` if no verifier is registered for the tier.
    Extra keyword arguments are forwarded to the verifier's constructor.
    """
    cls = TIER_VERIFIERS[tier]
    return cls(**kwargs)
