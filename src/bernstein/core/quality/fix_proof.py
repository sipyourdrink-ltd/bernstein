"""Bug-fix proof quality gate (``fix_proof``).

Proves that a bug-fix task's newly added or modified tests reproduce the bug,
pass with the fix, fail again when non-test source edits are reverted, and
introduce no regressions against pre-existing passing tests.

The gate evaluates three isolated git trees derived from the task diff plus the
base tree (arm 4 control/regression), executes each arm in a throwaway detached
worktree using a single recorded Python/pytest environment, and emits a
content-addressed receipt anchored on :class:`~bernstein.core.lineage.spine.LineageSpine`
for offline verification and deterministic re-run on the same tree OIDs.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from bernstein.core.lineage.spine import HASH_PROFILE_JCS_V2, LineageSpine, SpineStatus, content_hash_of
from bernstein.core.quality.flaky_detector import (
    FixProofObservations,
    FixProofVerdict,
    FlakyDetector,
    PytestInvocationObservation,
    PytestPhaseObservation,
    evaluate_fix_proof,
)
from bernstein.core.security.audit import load_or_create_audit_key
from bernstein.core.tasks.models import Task, TaskType

if TYPE_CHECKING:
    from collections.abc import Sequence

    from bernstein.core.quality.quality_gates import QualityGatesConfig

#: Issue-reference patterns in task titles/descriptions that identify a bug fix.
_BUG_ISSUE_RE = re.compile(
    r"\b(?:fix(?:e[sd])?|close[sd]?|resolve[sd]?|bug(?:fix)?)\s+(?:#\d+|https?://[^\s]+/issues/\d+)\b",
    re.IGNORECASE,
)

#: Candidate lockfiles inspected in order when fingerprinting the environment.
_LOCKFILE_CANDIDATES: tuple[str, ...] = ("uv.lock", "poetry.lock", "requirements.txt", "pyproject.toml")

#: Pinned git identity so detached worktree operations never fail cleanly in CI.
_GIT_IDENTITY_FLAGS: tuple[str, ...] = (
    "-c",
    "user.name=Bernstein FixProof",
    "-c",
    "user.email=fix-proof@bernstein.local",
    "-c",
    "commit.gpgsign=false",
)

#: Standalone pytest runner executed inside each isolated arm worktree.
#: Avoids importing ``bernstein`` inside the target tree so ``sys.modules``
#: resolves subjects strictly from the arm worktree under test.
_PYTEST_CAPTURE_SCRIPT = """
import json, sys, time
import pytest

out_path = sys.argv[1]
pytest_args = sys.argv[2:]
selected = []
phases = []

class _Plugin:
    def pytest_collection_finish(self, session):
        selected[:] = [item.nodeid for item in session.items]

    def pytest_collectreport(self, report):
        if report.failed:
            phases.append({"node_id": report.nodeid, "phase": "collect", "outcome": "error"})

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item, call):
        outcome_res = yield
        report = outcome_res.get_result()
        if hasattr(report, "wasxfail"):
            if report.skipped:
                outcome = "xfailed"
            elif report.passed or report.failed:
                outcome = "xpassed"
            else:
                outcome = "xfailed"
        elif report.passed:
            outcome = "passed"
        elif report.skipped:
            outcome = "skipped"
        elif report.when in ("setup", "teardown"):
            outcome = "error"
        elif call.excinfo is not None and issubclass(call.excinfo.type, AssertionError):
            outcome = "assertion_failed"
        else:
            outcome = "error"
        phases.append({"node_id": report.nodeid, "phase": report.when, "outcome": outcome})

started = time.perf_counter_ns()
exit_code = int(pytest.main(pytest_args, plugins=[_Plugin()]))
duration_ms = (time.perf_counter_ns() - started) // 1_000_000
payload = {
    "exit_code": exit_code,
    "selected_node_ids": selected,
    "phases": phases,
    "duration_ms": duration_ms,
}
with open(out_path, "w", encoding="utf-8") as fh:
    json.dump(payload, fh)
"""

ArmName = Literal["tests_on_base", "full_change", "source_reverted", "regression_base", "regression_full"]


@dataclass(frozen=True, slots=True)
class FixProofEnvironment:
    """Recorded execution environment shared by every arm of a proof run."""

    python_executable: str
    python_version: str
    lockfile_path: str
    lockfile_sha256: str
    proof_command: str
    regression_command: str

    def to_dict(self) -> dict[str, str]:
        return {
            "python_executable": self.python_executable,
            "python_version": self.python_version,
            "lockfile_path": self.lockfile_path,
            "lockfile_sha256": self.lockfile_sha256,
            "proof_command": self.proof_command,
            "regression_command": self.regression_command,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> FixProofEnvironment:
        return cls(
            python_executable=str(raw["python_executable"]),
            python_version=str(raw["python_version"]),
            lockfile_path=str(raw.get("lockfile_path", "")),
            lockfile_sha256=str(raw.get("lockfile_sha256", "")),
            proof_command=str(raw["proof_command"]),
            regression_command=str(raw["regression_command"]),
        )


@dataclass(frozen=True, slots=True)
class FixProofArmRun:
    """Single observed pytest invocation for one proof or regression arm."""

    exit_code: int
    duration_ms: int
    selected_node_ids: tuple[str, ...]
    phases: tuple[PytestPhaseObservation, ...]

    def to_observation(self) -> PytestInvocationObservation:
        return PytestInvocationObservation(
            exit_code=self.exit_code,
            selected_node_ids=self.selected_node_ids,
            phases=self.phases,
            duration_ms=self.duration_ms,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "selected_node_ids": list(self.selected_node_ids),
            "phases": [{"node_id": p.node_id, "phase": p.phase, "outcome": p.outcome} for p in self.phases],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> FixProofArmRun:
        phases = tuple(
            PytestPhaseObservation(
                node_id=str(p["node_id"]),
                phase=str(p["phase"]),  # type: ignore[arg-type]
                outcome=str(p["outcome"]),  # type: ignore[arg-type]
            )
            for p in raw.get("phases", [])
        )
        return cls(
            exit_code=int(raw["exit_code"]),
            duration_ms=int(raw["duration_ms"]),
            selected_node_ids=tuple(str(n) for n in raw.get("selected_node_ids", [])),
            phases=phases,
        )


@dataclass(frozen=True, slots=True)
class FixProofArmRecord:
    """All repeat runs recorded for a named arm against its git tree OID."""

    arm: ArmName
    tree_id: str
    runs: tuple[FixProofArmRun, ...]

    @property
    def exit_statuses(self) -> tuple[int, ...]:
        return tuple(run.exit_code for run in self.runs)

    @property
    def durations_ms(self) -> tuple[int, ...]:
        return tuple(run.duration_ms for run in self.runs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "arm": self.arm,
            "tree_id": self.tree_id,
            "exit_statuses": list(self.exit_statuses),
            "durations_ms": list(self.durations_ms),
            "runs": [run.to_dict() for run in self.runs],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> FixProofArmRecord:
        return cls(
            arm=str(raw["arm"]),  # type: ignore[arg-type]
            tree_id=str(raw["tree_id"]),
            runs=tuple(FixProofArmRun.from_dict(r) for r in raw.get("runs", [])),
        )


@dataclass(frozen=True, slots=True)
class FixProofReceipt:
    """Content-addressed proof receipt anchored onto the task's lineage chain."""

    task_id: str
    base_tree_id: str
    trees: dict[str, str]
    proof_node_ids: tuple[str, ...]
    regression_node_ids: tuple[str, ...]
    quarantined_node_ids: tuple[str, ...]
    repeats: int
    environment: FixProofEnvironment
    arms: dict[str, FixProofArmRecord]
    verdict: Literal["pass", "fail", "inconclusive"]
    reason: str
    proof_failure_ids: tuple[str, ...]
    regression_ids: tuple[str, ...]
    receipt_hash: str
    schema_version: int = 1
    lineage_entry_hash: str = ""
    inconclusive_code: str | None = None

    def seal_payload(self) -> dict[str, Any]:
        """Return canonical payload covered by :attr:`receipt_hash`."""
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "base_tree_id": self.base_tree_id,
            "trees": dict(self.trees),
            "proof_node_ids": list(self.proof_node_ids),
            "regression_node_ids": list(self.regression_node_ids),
            "quarantined_node_ids": list(self.quarantined_node_ids),
            "repeats": self.repeats,
            "environment": self.environment.to_dict(),
            "arms": {k: v.to_dict() for k, v in sorted(self.arms.items())},
            "verdict": self.verdict,
            "reason": self.reason,
            "proof_failure_ids": list(self.proof_failure_ids),
            "regression_ids": list(self.regression_ids),
            "inconclusive_code": self.inconclusive_code,
        }

    def verdict_fields(self) -> dict[str, Any]:
        """Return the commit-independent, duration-independent verdict identity."""
        return {
            "base_tree_id": self.base_tree_id,
            "trees": dict(self.trees),
            "proof_node_ids": list(self.proof_node_ids),
            "regression_node_ids": list(self.regression_node_ids),
            "verdict": self.verdict,
            "reason": self.reason,
            "proof_failure_ids": list(self.proof_failure_ids),
            "regression_ids": list(self.regression_ids),
        }

    def to_observations(self) -> FixProofObservations:
        """Convert stored arm runs back into :class:`FixProofObservations`."""
        return FixProofObservations(
            proof_node_ids=self.proof_node_ids,
            regression_node_ids=self.regression_node_ids,
            repeats=self.repeats,
            tests_on_base=tuple(r.to_observation() for r in self.arms["tests_on_base"].runs),
            full_change=tuple(r.to_observation() for r in self.arms["full_change"].runs),
            source_reverted=tuple(r.to_observation() for r in self.arms["source_reverted"].runs),
            regression_base=tuple(r.to_observation() for r in self.arms["regression_base"].runs),
            regression_full=tuple(r.to_observation() for r in self.arms["regression_full"].runs),
            quarantined_node_ids=self.quarantined_node_ids,
        )

    def to_dict(self) -> dict[str, Any]:
        payload = self.seal_payload()
        payload["receipt_hash"] = self.receipt_hash
        payload["lineage_entry_hash"] = self.lineage_entry_hash
        return payload

    def to_canonical_bytes(self) -> bytes:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> FixProofReceipt:
        return cls(
            schema_version=int(raw.get("schema_version", 1)),
            task_id=str(raw["task_id"]),
            base_tree_id=str(raw["base_tree_id"]),
            trees={str(k): str(v) for k, v in dict(raw["trees"]).items()},
            proof_node_ids=tuple(str(n) for n in raw.get("proof_node_ids", [])),
            regression_node_ids=tuple(str(n) for n in raw.get("regression_node_ids", [])),
            quarantined_node_ids=tuple(str(n) for n in raw.get("quarantined_node_ids", [])),
            repeats=int(raw["repeats"]),
            environment=FixProofEnvironment.from_dict(dict(raw["environment"])),
            arms={str(k): FixProofArmRecord.from_dict(dict(v)) for k, v in dict(raw["arms"]).items()},
            verdict=str(raw["verdict"]),  # type: ignore[arg-type]
            reason=str(raw["reason"]),
            proof_failure_ids=tuple(str(n) for n in raw.get("proof_failure_ids", [])),
            regression_ids=tuple(str(n) for n in raw.get("regression_ids", [])),
            receipt_hash=str(raw["receipt_hash"]),
            lineage_entry_hash=str(raw.get("lineage_entry_hash", "")),
            inconclusive_code=str(raw["inconclusive_code"]) if raw.get("inconclusive_code") is not None else None,
        )


def _compute_receipt_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def is_bug_fix_task(task: Task) -> bool:
    """Return True if *task* declares a bug-fix kind or references a fixed issue."""
    if task.task_type == TaskType.FIX:
        return True
    kind = str(task.metadata.get("task_kind") or task.metadata.get("kind") or "").strip().lower()
    if kind in {"bug", "bugfix", "bug-fix", "fix"}:
        return True
    for key in ("fixes_issue", "issue_number", "bug_id"):
        val = task.metadata.get(key)
        if val is not None and str(val).strip() != "":
            return True
    return bool(_BUG_ISSUE_RE.search(task.title) or _BUG_ISSUE_RE.search(task.description))


def is_fix_proof_enabled(config: QualityGatesConfig, task: Task) -> bool:
    """Resolve whether ``fix_proof`` is enabled for *task* under *config*."""
    override = task.metadata.get("fix_proof")
    if isinstance(override, bool):
        return override
    role = task.role.strip().lower()
    enabled_roles = {r.strip().lower() for r in getattr(config, "fix_proof_roles", []) if str(r).strip()}
    if role and role in enabled_roles:
        return True
    return bool(getattr(config, "fix_proof", False))


def _is_test_path(path: str) -> bool:
    posix = path.replace("\\", "/").lstrip("./")
    if not posix.endswith(".py"):
        return False
    parts = posix.split("/")
    filename = parts[-1]
    return (
        parts[0] in {"tests", "test"}
        or "tests" in parts[:-1]
        or filename.startswith("test_")
        or filename.endswith("_test.py")
    )


def _is_test_artifact(path: str) -> bool:
    posix = path.replace("\\", "/").lstrip("./")
    parts = posix.split("/")
    return _is_test_path(posix) or parts[0] in {"tests", "test"} or "tests" in parts[:-1]


def _safe_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return cleaned or "task"


def _run_git(
    cwd: Path,
    args: Sequence[str],
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    # Keep git deterministic even if inherited hooks/env vars exist.
    merged_env.pop("GIT_INDEX_FILE", None)
    merged_env.pop("GIT_DIR", None)
    merged_env.pop("GIT_WORK_TREE", None)
    if env:
        merged_env.update(env)
    return subprocess.run(
        ["git", *_GIT_IDENTITY_FLAGS, *args],
        cwd=cwd,
        env=merged_env,
        capture_output=True,
        text=True,
        check=False,
    )


def _resolve_git_top_level(path: Path) -> Path | None:
    res = _run_git(path, ["rev-parse", "--show-toplevel"])
    if res.returncode != 0 or not res.stdout.strip():
        return None
    return Path(res.stdout.strip())


def _resolve_base_commit(repo_root: Path, base_ref: str) -> str | None:
    for candidate in (base_ref, "HEAD~1", "HEAD"):
        res = _run_git(repo_root, ["rev-parse", "--verify", f"{candidate}^{{commit}}"])
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    return None


def _collect_changed_paths(run_dir: Path, base_commit: str) -> list[str]:
    changed: set[str] = set()
    diff_res = _run_git(run_dir, ["diff", "--name-only", "-z", base_commit, "HEAD"])
    if diff_res.returncode == 0 and diff_res.stdout:
        for entry in diff_res.stdout.split("\0"):
            if entry:
                changed.add(entry)
    status_res = _run_git(run_dir, ["status", "--porcelain=v1", "-uall", "-z"])
    if status_res.returncode == 0 and status_res.stdout:
        tokens = status_res.stdout.split("\0")
        idx = 0
        while idx < len(tokens):
            entry = tokens[idx]
            idx += 1
            if len(entry) < 4:
                continue
            code = entry[:2]
            path = entry[3:]
            if path and not path.startswith(".sdd/"):
                changed.add(path)
            if ("R" in code or "C" in code) and idx < len(tokens):
                dest = tokens[idx]
                idx += 1
                if dest and not dest.startswith(".sdd/"):
                    changed.add(dest)
    return sorted(p for p in changed if not p.startswith(".sdd/") and p != ".sdd")


def _sync_paths_from_run_dir(run_dir: Path, wt_dir: Path, paths: Sequence[str]) -> None:
    for rel in paths:
        src = run_dir / rel
        dst = wt_dir / rel
        if src.is_file() or src.is_symlink():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        elif src.is_dir():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
        else:
            if dst.is_file() or dst.is_symlink():
                dst.unlink()
            elif dst.is_dir():
                shutil.rmtree(dst)


def _write_worktree_tree_id(wt_dir: Path) -> str:
    add_res = _run_git(wt_dir, ["add", "-A"])
    if add_res.returncode != 0:
        raise RuntimeError(f"git add -A failed: {add_res.stderr.strip()}")
    tree_res = _run_git(wt_dir, ["write-tree"])
    if tree_res.returncode != 0 or not tree_res.stdout.strip():
        raise RuntimeError(f"git write-tree failed: {tree_res.stderr.strip()}")
    return tree_res.stdout.strip()


def _create_detached_worktree(repo_root: Path, wt_dir: Path, commit_oid: str) -> None:
    res = _run_git(repo_root, ["worktree", "add", "--detach", str(wt_dir), commit_oid])
    if res.returncode != 0:
        raise RuntimeError(f"git worktree add failed for {commit_oid}: {res.stderr.strip()}")


def _checkout_tree_in_worktree(repo_root: Path, wt_dir: Path, tree_id: str, seed_commit: str) -> None:
    _create_detached_worktree(repo_root, wt_dir, seed_commit)
    # Clean tracked/untracked before overlaying the exact recorded git tree OID.
    clean_res = _run_git(wt_dir, ["clean", "-fd"])
    if clean_res.returncode != 0:
        raise RuntimeError(f"git clean failed: {clean_res.stderr.strip()}")
    read_res = _run_git(wt_dir, ["read-tree", "--reset", "-u", tree_id])
    if read_res.returncode != 0:
        raise RuntimeError(f"git read-tree failed for {tree_id}: {read_res.stderr.strip()}")


def _remove_worktree(repo_root: Path, wt_dir: Path) -> None:
    _run_git(repo_root, ["worktree", "remove", "--force", str(wt_dir)])
    if wt_dir.exists():
        shutil.rmtree(wt_dir, ignore_errors=True)
    _run_git(repo_root, ["worktree", "prune"])


def _fingerprint_environment(
    worktree: Path,
    proof_cmd: str,
    regression_cmd: str,
    python_exe: str,
) -> FixProofEnvironment:
    lockfile_path = ""
    lockfile_sha256 = ""
    for candidate in _LOCKFILE_CANDIDATES:
        target = worktree / candidate
        if target.is_file():
            lockfile_path = candidate
            lockfile_sha256 = hashlib.sha256(target.read_bytes()).hexdigest()
            break
    return FixProofEnvironment(
        python_executable=python_exe,
        python_version=platform.python_version(),
        lockfile_path=lockfile_path,
        lockfile_sha256=lockfile_sha256,
        proof_command=proof_cmd,
        regression_command=regression_cmd,
    )


def _parse_pytest_command(command: str) -> tuple[str, list[str], str]:
    """Return ``(python_exe, base_args, display_command)`` for *command*."""
    stripped = command.strip()
    if not stripped:
        return sys.executable, [], f"{sys.executable} -m pytest"
    tokens = shlex.split(stripped)
    python_exe = sys.executable
    base_args: list[str] = []
    if len(tokens) >= 3 and tokens[1] == "-m" and tokens[2] == "pytest":
        python_exe = tokens[0]
        base_args = tokens[3:]
    elif tokens and tokens[0] in {"pytest", "py.test"}:
        base_args = tokens[1:]
    elif len(tokens) >= 3 and tokens[:3] == ["uv", "run", "pytest"]:
        base_args = tokens[3:]
    else:
        base_args = tokens
    return python_exe, base_args, stripped


def _subprocess_env(worktree: Path) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper()
        in {"PATH", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "VIRTUAL_ENV", "HOME", "PYTEST_DISABLE_PLUGIN_AUTOLOAD"}
    }
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # Allow src layout inside isolated repos when no editable install points there.
    src_dir = worktree / "src"
    extra = [str(worktree)]
    if src_dir.is_dir():
        extra.insert(0, str(src_dir))
    env["PYTHONPATH"] = os.pathsep.join(extra)
    return env


def _invoke_pytest_capture(
    worktree: Path,
    *,
    python_exe: str,
    pytest_args: Sequence[str],
    timeout_s: int,
    tmp_dir: Path,
    run_tag: str,
) -> tuple[FixProofArmRun | None, str | None, str | None]:
    """Run pytest cleanly inside *worktree*, capturing structured JSON output.

    Returns ``(arm_run, inconclusive_code, error_detail)``.
    """
    out_file = tmp_dir / f"{run_tag}.json"
    try:
        proc = subprocess.run(
            [python_exe, "-c", _PYTEST_CAPTURE_SCRIPT, str(out_file), *pytest_args],
            cwd=worktree,
            env=_subprocess_env(worktree),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except FileNotFoundError:
        return None, "command-not-found", f"Python interpreter not found: {python_exe!r}"
    except subprocess.TimeoutExpired:
        return None, "runner-died-before-output", f"pytest timed out after {timeout_s}s"

    if not out_file.is_file():
        detail = (proc.stderr or proc.stdout or f"exit code {proc.returncode}").strip()
        return None, "runner-died-before-output", f"pytest exited without observation output: {detail}"
    try:
        raw = json.loads(out_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, "evidence-unreadable", f"could not decode pytest observation: {exc}"
    if not isinstance(raw, dict):
        return None, "evidence-unreadable", "pytest observation JSON payload was not an object"

    phases = tuple(
        PytestPhaseObservation(
            node_id=str(p["node_id"]),
            phase=str(p["phase"]),  # type: ignore[arg-type]
            outcome=str(p["outcome"]),  # type: ignore[arg-type]
        )
        for p in raw.get("phases", [])
    )
    selected = tuple(str(n) for n in raw.get("selected_node_ids", []))
    return (
        FixProofArmRun(
            exit_code=int(raw.get("exit_code", proc.returncode)),
            duration_ms=int(raw.get("duration_ms", 0)),
            selected_node_ids=selected,
            phases=phases,
        ),
        None,
        None,
    )


def _collect_node_ids(
    worktree: Path,
    *,
    python_exe: str,
    pytest_args: Sequence[str],
    timeout_s: int,
    tmp_dir: Path,
    tag: str,
) -> tuple[tuple[str, ...], str | None, str | None]:
    run, err_code, err_detail = _invoke_pytest_capture(
        worktree,
        python_exe=python_exe,
        pytest_args=["--collect-only", "-q", *pytest_args],
        timeout_s=timeout_s,
        tmp_dir=tmp_dir,
        run_tag=tag,
    )
    if run is None:
        return (), err_code, err_detail
    if any(phase.outcome == "error" for phase in run.phases):
        return (
            (),
            "evidence-unreadable",
            f"pytest collection errored for selection {list(pytest_args)!r}",
        )
    if run.exit_code not in (0, 5):
        return (
            (),
            "runner-died-before-output",
            f"pytest collection failed with exit code {run.exit_code}",
        )
    deduped = tuple(dict.fromkeys(run.selected_node_ids))
    return deduped, None, None


def _run_repeated_arm(
    worktree: Path,
    *,
    arm: ArmName,
    tree_id: str,
    python_exe: str,
    base_args: Sequence[str],
    node_ids: Sequence[str],
    repeats: int,
    timeout_s: int,
    tmp_dir: Path,
) -> tuple[FixProofArmRecord, str | None, str | None]:
    runs: list[FixProofArmRun] = []
    # Strip target-like positional path filters when explicit node_ids are frozen.
    args = [*base_args, *node_ids] if node_ids else list(base_args)
    for idx in range(repeats):
        arm_run, err_code, err_detail = _invoke_pytest_capture(
            worktree,
            python_exe=python_exe,
            pytest_args=args,
            timeout_s=timeout_s,
            tmp_dir=tmp_dir,
            run_tag=f"{arm}_{idx}",
        )
        if arm_run is None:
            return FixProofArmRecord(arm=arm, tree_id=tree_id, runs=tuple(runs)), err_code, err_detail
        runs.append(arm_run)
    return FixProofArmRecord(arm=arm, tree_id=tree_id, runs=tuple(runs)), None, None


def _anchor_receipt_to_lineage(
    workdir: Path,
    *,
    task_id: str,
    receipt_bytes: bytes,
    hmac_key: bytes | None,
) -> str:
    key = hmac_key if hmac_key is not None else load_or_create_audit_key()
    safe_task = _safe_id(task_id)
    run_id = f"fix-proof-{safe_task}"
    spine = LineageSpine(workdir / ".sdd" / "lineage", run_id=run_id, hmac_key=key, hash_profile=HASH_PROFILE_JCS_V2)
    return spine.record(
        artifact_path=f"fix_proof/{safe_task}.json",
        content=receipt_bytes,
        actor="fix_proof_gate",
        step_id=safe_task,
        model="deterministic",
        timestamp=0,
    )


def _persist_receipt(workdir: Path, task_id: str, receipt: FixProofReceipt) -> Path:
    out_dir = workdir / ".sdd" / "runtime" / "fix_proof"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{_safe_id(task_id)}.json"
    out_path.write_text(
        json.dumps(receipt.to_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return out_path


def _empty_arm(arm: ArmName, tree_id: str = "") -> FixProofArmRecord:
    return FixProofArmRecord(arm=arm, tree_id=tree_id, runs=())


def _build_receipt(
    *,
    task_id: str,
    base_tree_id: str,
    trees: dict[str, str],
    proof_node_ids: tuple[str, ...],
    regression_node_ids: tuple[str, ...],
    quarantined_node_ids: tuple[str, ...],
    repeats: int,
    environment: FixProofEnvironment,
    arms: dict[str, FixProofArmRecord],
    verdict: FixProofVerdict,
    inconclusive_code: str | None,
    workdir: Path,
    hmac_key: bytes | None,
) -> FixProofReceipt:
    payload = {
        "schema_version": 1,
        "task_id": task_id,
        "base_tree_id": base_tree_id,
        "trees": dict(trees),
        "proof_node_ids": list(proof_node_ids),
        "regression_node_ids": list(regression_node_ids),
        "quarantined_node_ids": list(quarantined_node_ids),
        "repeats": repeats,
        "environment": environment.to_dict(),
        "arms": {k: v.to_dict() for k, v in sorted(arms.items())},
        "verdict": verdict.verdict,
        "reason": verdict.reason,
        "proof_failure_ids": list(verdict.proof_failure_ids),
        "regression_ids": list(verdict.regression_ids),
        "inconclusive_code": inconclusive_code,
    }
    digest = _compute_receipt_hash(payload)
    sealed = FixProofReceipt(
        task_id=task_id,
        base_tree_id=base_tree_id,
        trees=trees,
        proof_node_ids=proof_node_ids,
        regression_node_ids=regression_node_ids,
        quarantined_node_ids=quarantined_node_ids,
        repeats=repeats,
        environment=environment,
        arms=arms,
        verdict=verdict.verdict,
        reason=verdict.reason,
        proof_failure_ids=verdict.proof_failure_ids,
        regression_ids=verdict.regression_ids,
        receipt_hash=digest,
        inconclusive_code=inconclusive_code,
    )
    entry_hash = _anchor_receipt_to_lineage(
        workdir,
        task_id=task_id,
        receipt_bytes=sealed.to_canonical_bytes(),
        hmac_key=hmac_key,
    )
    final_receipt = FixProofReceipt(
        schema_version=sealed.schema_version,
        task_id=sealed.task_id,
        base_tree_id=sealed.base_tree_id,
        trees=sealed.trees,
        proof_node_ids=sealed.proof_node_ids,
        regression_node_ids=sealed.regression_node_ids,
        quarantined_node_ids=sealed.quarantined_node_ids,
        repeats=sealed.repeats,
        environment=sealed.environment,
        arms=sealed.arms,
        verdict=sealed.verdict,
        reason=sealed.reason,
        proof_failure_ids=sealed.proof_failure_ids,
        regression_ids=sealed.regression_ids,
        receipt_hash=sealed.receipt_hash,
        lineage_entry_hash=entry_hash,
        inconclusive_code=sealed.inconclusive_code,
    )
    _persist_receipt(workdir, task_id, final_receipt)
    return final_receipt


def _execute_arms_on_worktrees(
    *,
    workdir: Path,
    task_id: str,
    base_tree_id: str,
    wt_base: Path,
    wt_arm1: Path,
    tree_arm1: str,
    wt_arm2: Path,
    tree_arm2: str,
    wt_arm3: Path,
    tree_arm3: str,
    changed_test_paths: Sequence[str],
    repeats: int,
    timeout_s: int,
    proof_python: str,
    proof_base_args: Sequence[str],
    proof_cmd_str: str,
    reg_python: str,
    reg_base_args: Sequence[str],
    reg_cmd_str: str,
    regression_paths: Sequence[str],
    quarantined_node_ids: tuple[str, ...],
    tmp_dir: Path,
    hmac_key: bytes | None,
    preset_proof_node_ids: tuple[str, ...] | None = None,
    preset_regression_node_ids: tuple[str, ...] | None = None,
) -> FixProofReceipt:
    environment = _fingerprint_environment(wt_arm2, proof_cmd_str, reg_cmd_str, proof_python)
    trees = {
        "tests_on_base": tree_arm1,
        "full_change": tree_arm2,
        "source_reverted": tree_arm3,
    }
    arms: dict[str, FixProofArmRecord] = {
        "tests_on_base": _empty_arm("tests_on_base", tree_arm1),
        "full_change": _empty_arm("full_change", tree_arm2),
        "source_reverted": _empty_arm("source_reverted", tree_arm3),
        "regression_base": _empty_arm("regression_base", base_tree_id),
        "regression_full": _empty_arm("regression_full", tree_arm2),
    }

    # Freeze proof test selection before arm 1 executes.
    if preset_proof_node_ids is not None:
        proof_node_ids = preset_proof_node_ids
    elif not changed_test_paths:
        return _build_receipt(
            task_id=task_id,
            base_tree_id=base_tree_id,
            trees=trees,
            proof_node_ids=(),
            regression_node_ids=(),
            quarantined_node_ids=quarantined_node_ids,
            repeats=repeats,
            environment=environment,
            arms=arms,
            verdict=FixProofVerdict("fail", "changed diff does not include any proof tests"),
            inconclusive_code=None,
            workdir=workdir,
            hmac_key=hmac_key,
        )
    else:
        proof_node_ids, err_code, err_detail = _collect_node_ids(
            wt_arm2,
            python_exe=proof_python,
            pytest_args=[*proof_base_args, *changed_test_paths],
            timeout_s=timeout_s,
            tmp_dir=tmp_dir,
            tag="collect_proof",
        )
        if err_code is not None:
            return _build_receipt(
                task_id=task_id,
                base_tree_id=base_tree_id,
                trees=trees,
                proof_node_ids=(),
                regression_node_ids=(),
                quarantined_node_ids=quarantined_node_ids,
                repeats=repeats,
                environment=environment,
                arms=arms,
                verdict=FixProofVerdict("inconclusive", err_detail or "proof collection failed"),
                inconclusive_code=err_code,
                workdir=workdir,
                hmac_key=hmac_key,
            )

    if not proof_node_ids:
        return _build_receipt(
            task_id=task_id,
            base_tree_id=base_tree_id,
            trees=trees,
            proof_node_ids=(),
            regression_node_ids=(),
            quarantined_node_ids=quarantined_node_ids,
            repeats=repeats,
            environment=environment,
            arms=arms,
            verdict=FixProofVerdict("fail", "no proof tests collected from task diff"),
            inconclusive_code=None,
            workdir=workdir,
            hmac_key=hmac_key,
        )

    # Refuse immediately if any proof test is already quarantined by flaky_detector.
    if set(proof_node_ids) & set(quarantined_node_ids):
        evidence = FixProofObservations(
            proof_node_ids=proof_node_ids,
            regression_node_ids=(),
            repeats=repeats,
            tests_on_base=(),
            full_change=(),
            source_reverted=(),
            regression_base=(),
            regression_full=(),
            quarantined_node_ids=quarantined_node_ids,
        )
        verdict = evaluate_fix_proof(evidence)
        return _build_receipt(
            task_id=task_id,
            base_tree_id=base_tree_id,
            trees=trees,
            proof_node_ids=proof_node_ids,
            regression_node_ids=(),
            quarantined_node_ids=quarantined_node_ids,
            repeats=repeats,
            environment=environment,
            arms=arms,
            verdict=verdict,
            inconclusive_code="evidence-missing",
            workdir=workdir,
            hmac_key=hmac_key,
        )

    proof_flags = [a for a in proof_base_args if a.startswith("-")]
    reg_flags = [a for a in reg_base_args if a.startswith("-")]

    # Arm 1: base + tests only.
    arm1_rec, err_code, err_detail = _run_repeated_arm(
        wt_arm1,
        arm="tests_on_base",
        tree_id=tree_arm1,
        python_exe=proof_python,
        base_args=proof_flags,
        node_ids=proof_node_ids,
        repeats=repeats,
        timeout_s=timeout_s,
        tmp_dir=tmp_dir,
    )
    arms["tests_on_base"] = arm1_rec
    if err_code is not None:
        return _build_receipt(
            task_id=task_id,
            base_tree_id=base_tree_id,
            trees=trees,
            proof_node_ids=proof_node_ids,
            regression_node_ids=(),
            quarantined_node_ids=quarantined_node_ids,
            repeats=repeats,
            environment=environment,
            arms=arms,
            verdict=FixProofVerdict("inconclusive", err_detail or "tests_on_base arm errored"),
            inconclusive_code=err_code,
            workdir=workdir,
            hmac_key=hmac_key,
        )

    # Arm 2: full change.
    arm2_rec, err_code, err_detail = _run_repeated_arm(
        wt_arm2,
        arm="full_change",
        tree_id=tree_arm2,
        python_exe=proof_python,
        base_args=proof_flags,
        node_ids=proof_node_ids,
        repeats=repeats,
        timeout_s=timeout_s,
        tmp_dir=tmp_dir,
    )
    arms["full_change"] = arm2_rec
    if err_code is not None:
        return _build_receipt(
            task_id=task_id,
            base_tree_id=base_tree_id,
            trees=trees,
            proof_node_ids=proof_node_ids,
            regression_node_ids=(),
            quarantined_node_ids=quarantined_node_ids,
            repeats=repeats,
            environment=environment,
            arms=arms,
            verdict=FixProofVerdict("inconclusive", err_detail or "full_change arm errored"),
            inconclusive_code=err_code,
            workdir=workdir,
            hmac_key=hmac_key,
        )

    # Arm 3: full change with non-test source edits reverted.
    arm3_rec, err_code, err_detail = _run_repeated_arm(
        wt_arm3,
        arm="source_reverted",
        tree_id=tree_arm3,
        python_exe=proof_python,
        base_args=proof_flags,
        node_ids=proof_node_ids,
        repeats=repeats,
        timeout_s=timeout_s,
        tmp_dir=tmp_dir,
    )
    arms["source_reverted"] = arm3_rec
    if err_code is not None:
        return _build_receipt(
            task_id=task_id,
            base_tree_id=base_tree_id,
            trees=trees,
            proof_node_ids=proof_node_ids,
            regression_node_ids=(),
            quarantined_node_ids=quarantined_node_ids,
            repeats=repeats,
            environment=environment,
            arms=arms,
            verdict=FixProofVerdict("inconclusive", err_detail or "source_reverted arm errored"),
            inconclusive_code=err_code,
            workdir=workdir,
            hmac_key=hmac_key,
        )

    # Freeze regression selection before arm 4 runs.
    if preset_regression_node_ids is not None:
        regression_node_ids = preset_regression_node_ids
    else:
        reg_collect_args = [*reg_base_args, *regression_paths] if regression_paths else list(reg_base_args)
        regression_node_ids, err_code, err_detail = _collect_node_ids(
            wt_base,
            python_exe=reg_python,
            pytest_args=reg_collect_args,
            timeout_s=timeout_s,
            tmp_dir=tmp_dir,
            tag="collect_regression",
        )
        if err_code is not None:
            return _build_receipt(
                task_id=task_id,
                base_tree_id=base_tree_id,
                trees=trees,
                proof_node_ids=proof_node_ids,
                regression_node_ids=(),
                quarantined_node_ids=quarantined_node_ids,
                repeats=repeats,
                environment=environment,
                arms=arms,
                verdict=FixProofVerdict("inconclusive", err_detail or "regression collection failed"),
                inconclusive_code=err_code,
                workdir=workdir,
                hmac_key=hmac_key,
            )

    # Arm 4a: regression selection on base tree.
    reg_base_rec, err_code, err_detail = _run_repeated_arm(
        wt_base,
        arm="regression_base",
        tree_id=base_tree_id,
        python_exe=reg_python,
        base_args=reg_flags,
        node_ids=regression_node_ids,
        repeats=repeats,
        timeout_s=timeout_s,
        tmp_dir=tmp_dir,
    )
    arms["regression_base"] = reg_base_rec
    if err_code is not None:
        return _build_receipt(
            task_id=task_id,
            base_tree_id=base_tree_id,
            trees=trees,
            proof_node_ids=proof_node_ids,
            regression_node_ids=regression_node_ids,
            quarantined_node_ids=quarantined_node_ids,
            repeats=repeats,
            environment=environment,
            arms=arms,
            verdict=FixProofVerdict("inconclusive", err_detail or "regression_base arm errored"),
            inconclusive_code=err_code,
            workdir=workdir,
            hmac_key=hmac_key,
        )

    # Arm 4b: identical regression selection on full change tree.
    reg_full_rec, err_code, err_detail = _run_repeated_arm(
        wt_arm2,
        arm="regression_full",
        tree_id=tree_arm2,
        python_exe=reg_python,
        base_args=reg_flags,
        node_ids=regression_node_ids,
        repeats=repeats,
        timeout_s=timeout_s,
        tmp_dir=tmp_dir,
    )
    arms["regression_full"] = reg_full_rec
    if err_code is not None:
        return _build_receipt(
            task_id=task_id,
            base_tree_id=base_tree_id,
            trees=trees,
            proof_node_ids=proof_node_ids,
            regression_node_ids=regression_node_ids,
            quarantined_node_ids=quarantined_node_ids,
            repeats=repeats,
            environment=environment,
            arms=arms,
            verdict=FixProofVerdict("inconclusive", err_detail or "regression_full arm errored"),
            inconclusive_code=err_code,
            workdir=workdir,
            hmac_key=hmac_key,
        )

    evidence = FixProofObservations(
        proof_node_ids=proof_node_ids,
        regression_node_ids=regression_node_ids,
        repeats=repeats,
        tests_on_base=tuple(r.to_observation() for r in arm1_rec.runs),
        full_change=tuple(r.to_observation() for r in arm2_rec.runs),
        source_reverted=tuple(r.to_observation() for r in arm3_rec.runs),
        regression_base=tuple(r.to_observation() for r in reg_base_rec.runs),
        regression_full=tuple(r.to_observation() for r in reg_full_rec.runs),
        quarantined_node_ids=quarantined_node_ids,
    )
    verdict = evaluate_fix_proof(evidence)
    inconclusive_code = "evidence-missing" if verdict.verdict == "inconclusive" else None
    return _build_receipt(
        task_id=task_id,
        base_tree_id=base_tree_id,
        trees=trees,
        proof_node_ids=proof_node_ids,
        regression_node_ids=regression_node_ids,
        quarantined_node_ids=quarantined_node_ids,
        repeats=repeats,
        environment=environment,
        arms=arms,
        verdict=verdict,
        inconclusive_code=inconclusive_code,
        workdir=workdir,
        hmac_key=hmac_key,
    )


def run_fix_proof(
    *,
    task: Task,
    workdir: Path,
    run_dir: Path,
    base_ref: str = "HEAD",
    repeats: int = 2,
    timeout_s: int = 120,
    proof_command: str = "",
    regression_command: str = "",
    regression_paths: Sequence[str] = (),
    flaky_min_runs: int = 5,
    flaky_threshold: float = 0.15,
    hmac_key: bytes | None = None,
) -> FixProofReceipt:
    """Execute the 4-arm ``fix_proof`` check in isolated throwaway worktrees."""
    repo_root = _resolve_git_top_level(run_dir) or _resolve_git_top_level(workdir)
    proof_python, proof_base_args, proof_cmd_str = _parse_pytest_command(proof_command)
    reg_cmd_raw = regression_command or proof_command
    reg_python, reg_base_args, reg_cmd_str = _parse_pytest_command(reg_cmd_raw)
    if reg_python != proof_python:
        # Enforce the same Python interpreter across all arms.
        reg_python = proof_python
    if repo_root is None:
        env = _fingerprint_environment(run_dir, proof_cmd_str, reg_cmd_str, proof_python)
        return _build_receipt(
            task_id=task.id,
            base_tree_id="",
            trees={"tests_on_base": "", "full_change": "", "source_reverted": ""},
            proof_node_ids=(),
            regression_node_ids=(),
            quarantined_node_ids=(),
            repeats=repeats,
            environment=env,
            arms={
                "tests_on_base": _empty_arm("tests_on_base"),
                "full_change": _empty_arm("full_change"),
                "source_reverted": _empty_arm("source_reverted"),
                "regression_base": _empty_arm("regression_base"),
                "regression_full": _empty_arm("regression_full"),
            },
            verdict=FixProofVerdict("inconclusive", "workdir is not inside a git repository"),
            inconclusive_code="evidence-missing",
            workdir=workdir,
            hmac_key=hmac_key,
        )

    base_commit = _resolve_base_commit(repo_root, base_ref)
    if base_commit is None:
        env = _fingerprint_environment(run_dir, proof_cmd_str, reg_cmd_str, proof_python)
        return _build_receipt(
            task_id=task.id,
            base_tree_id="",
            trees={"tests_on_base": "", "full_change": "", "source_reverted": ""},
            proof_node_ids=(),
            regression_node_ids=(),
            quarantined_node_ids=(),
            repeats=repeats,
            environment=env,
            arms={
                "tests_on_base": _empty_arm("tests_on_base"),
                "full_change": _empty_arm("full_change"),
                "source_reverted": _empty_arm("source_reverted"),
                "regression_base": _empty_arm("regression_base"),
                "regression_full": _empty_arm("regression_full"),
            },
            verdict=FixProofVerdict("inconclusive", f"could not resolve base ref {base_ref!r}"),
            inconclusive_code="evidence-missing",
            workdir=workdir,
            hmac_key=hmac_key,
        )

    base_tree_res = _run_git(repo_root, ["rev-parse", f"{base_commit}^{{tree}}"])
    base_tree_id = base_tree_res.stdout.strip()
    changed_paths = _collect_changed_paths(run_dir, base_commit)
    changed_test_paths = [p for p in changed_paths if _is_test_path(p)]
    changed_test_artifacts = [p for p in changed_paths if _is_test_artifact(p)]
    changed_source_paths = [p for p in changed_paths if not _is_test_artifact(p)]

    quarantined = tuple(
        FlakyDetector(workdir, min_runs=flaky_min_runs, flaky_threshold=flaky_threshold).get_quarantined()
    )

    with tempfile.TemporaryDirectory(prefix="bernstein-fix-proof-") as td:
        tmp_dir = Path(td)
        wt_base = tmp_dir / "wt_base"
        wt_arm1 = tmp_dir / "wt_arm1_base_plus_tests"
        wt_arm2 = tmp_dir / "wt_arm2_full"
        wt_arm3 = tmp_dir / "wt_arm3_source_reverted"
        try:
            _create_detached_worktree(repo_root, wt_base, base_commit)
            _create_detached_worktree(repo_root, wt_arm1, base_commit)
            _sync_paths_from_run_dir(run_dir, wt_arm1, changed_test_artifacts)
            tree_arm1 = _write_worktree_tree_id(wt_arm1)

            _create_detached_worktree(repo_root, wt_arm2, base_commit)
            _sync_paths_from_run_dir(run_dir, wt_arm2, changed_paths)
            tree_arm2 = _write_worktree_tree_id(wt_arm2)

            _create_detached_worktree(repo_root, wt_arm3, base_commit)
            _sync_paths_from_run_dir(run_dir, wt_arm3, changed_paths)
            # Revert non-test source edits back to base_commit for arm 3.
            _sync_paths_from_run_dir(wt_base, wt_arm3, changed_source_paths)
            tree_arm3 = _write_worktree_tree_id(wt_arm3)

            default_reg_paths = list(regression_paths)
            if not default_reg_paths and not reg_base_args:
                default_reg_paths = ["tests"] if (wt_base / "tests").is_dir() else ["."]

            return _execute_arms_on_worktrees(
                workdir=workdir,
                task_id=task.id,
                base_tree_id=base_tree_id,
                wt_base=wt_base,
                wt_arm1=wt_arm1,
                tree_arm1=tree_arm1,
                wt_arm2=wt_arm2,
                tree_arm2=tree_arm2,
                wt_arm3=wt_arm3,
                tree_arm3=tree_arm3,
                changed_test_paths=changed_test_paths,
                repeats=max(1, repeats),
                timeout_s=timeout_s,
                proof_python=proof_python,
                proof_base_args=proof_base_args,
                proof_cmd_str=proof_cmd_str,
                reg_python=reg_python,
                reg_base_args=reg_base_args,
                reg_cmd_str=reg_cmd_str,
                regression_paths=default_reg_paths,
                quarantined_node_ids=quarantined,
                tmp_dir=tmp_dir,
                hmac_key=hmac_key,
            )
        finally:
            for wt in (wt_arm3, wt_arm2, wt_arm1, wt_base):
                _remove_worktree(repo_root, wt)


def verify_fix_proof_receipt(
    receipt: FixProofReceipt,
    *,
    lineage_root: Path | None = None,
    hmac_key: bytes | None = None,
) -> tuple[bool, list[str]]:
    """Offline verification of a sealed :class:`FixProofReceipt`.

    Checks content-addressed digest integrity, re-derives the verdict directly
    from the stored per-arm observations via :func:`evaluate_fix_proof`, and
    optionally verifies the Merkle+HMAC :class:`LineageSpine` entry.
    """
    errors: list[str] = []
    expected_hash = _compute_receipt_hash(receipt.seal_payload())
    if not _hmac.compare_digest(receipt.receipt_hash, expected_hash):
        errors.append("receipt_hash mismatch: receipt contents were modified")

    if (set(receipt.proof_node_ids) & set(receipt.quarantined_node_ids)) and receipt.verdict != "inconclusive":
        errors.append("verdict mismatch: proof selection includes a quarantined test so verdict must be inconclusive")

    # Re-derive the verdict offline from stored arm observations when arms ran.
    required_arms = ("tests_on_base", "full_change", "source_reverted", "regression_base", "regression_full")
    if all(receipt.arms[name].runs for name in required_arms):
        rederived = evaluate_fix_proof(receipt.to_observations())
        if rederived.verdict != receipt.verdict:
            errors.append(f"verdict mismatch: stored {receipt.verdict!r}, rederived {rederived.verdict!r}")
        if rederived.proof_failure_ids != receipt.proof_failure_ids:
            errors.append("proof_failure_ids mismatch with stored arm observations")
        if rederived.regression_ids != receipt.regression_ids:
            errors.append("regression_ids mismatch with stored arm observations")

    if lineage_root is not None:
        key = hmac_key if hmac_key is not None else load_or_create_audit_key()
        run_id = f"fix-proof-{_safe_id(receipt.task_id)}"
        spine = LineageSpine(lineage_root, run_id=run_id, hmac_key=key, hash_profile=HASH_PROFILE_JCS_V2)
        spine_result = spine.verify()
        if not spine_result.ok or spine_result.status is not SpineStatus.OK:
            errors.append(f"lineage verification failed ({spine_result.status.value}): {spine_result.errors}")
        else:
            sealed_copy = FixProofReceipt(
                schema_version=receipt.schema_version,
                task_id=receipt.task_id,
                base_tree_id=receipt.base_tree_id,
                trees=receipt.trees,
                proof_node_ids=receipt.proof_node_ids,
                regression_node_ids=receipt.regression_node_ids,
                quarantined_node_ids=receipt.quarantined_node_ids,
                repeats=receipt.repeats,
                environment=receipt.environment,
                arms=receipt.arms,
                verdict=receipt.verdict,
                reason=receipt.reason,
                proof_failure_ids=receipt.proof_failure_ids,
                regression_ids=receipt.regression_ids,
                receipt_hash=receipt.receipt_hash,
                lineage_entry_hash="",
                inconclusive_code=receipt.inconclusive_code,
            )
            expected_content_hash = content_hash_of(sealed_copy.to_canonical_bytes())
            entries = list(spine.iter_entries())
            matched = any(
                e.entry_hash == receipt.lineage_entry_hash and e.content_hash == expected_content_hash for e in entries
            )
            if not matched:
                errors.append("receipt lineage_entry_hash or content_hash not found in LineageSpine")

    return len(errors) == 0, errors


def rerun_fix_proof_on_trees(
    repo_root: Path,
    receipt: FixProofReceipt,
    *,
    workdir: Path | None = None,
    timeout_s: int = 120,
    hmac_key: bytes | None = None,
) -> FixProofReceipt:
    """Re-run a sealed receipt offline from its recorded git tree IDs only."""
    target_workdir = workdir or repo_root
    seed_commit = _resolve_base_commit(repo_root, "HEAD")
    if seed_commit is None:
        raise RuntimeError(f"cannot resolve a seed commit in {repo_root}")

    proof_python, proof_base_args, proof_cmd_str = _parse_pytest_command(receipt.environment.proof_command)
    reg_python, reg_base_args, reg_cmd_str = _parse_pytest_command(receipt.environment.regression_command)
    if reg_python != proof_python:
        reg_python = proof_python

    with tempfile.TemporaryDirectory(prefix="bernstein-fix-proof-rerun-") as td:
        tmp_dir = Path(td)
        wt_base = tmp_dir / "wt_base"
        wt_arm1 = tmp_dir / "wt_arm1"
        wt_arm2 = tmp_dir / "wt_arm2"
        wt_arm3 = tmp_dir / "wt_arm3"
        try:
            _checkout_tree_in_worktree(repo_root, wt_base, receipt.base_tree_id, seed_commit)
            _checkout_tree_in_worktree(repo_root, wt_arm1, receipt.trees["tests_on_base"], seed_commit)
            _checkout_tree_in_worktree(repo_root, wt_arm2, receipt.trees["full_change"], seed_commit)
            _checkout_tree_in_worktree(repo_root, wt_arm3, receipt.trees["source_reverted"], seed_commit)

            return _execute_arms_on_worktrees(
                workdir=target_workdir,
                task_id=receipt.task_id,
                base_tree_id=receipt.base_tree_id,
                wt_base=wt_base,
                wt_arm1=wt_arm1,
                tree_arm1=receipt.trees["tests_on_base"],
                wt_arm2=wt_arm2,
                tree_arm2=receipt.trees["full_change"],
                wt_arm3=wt_arm3,
                tree_arm3=receipt.trees["source_reverted"],
                changed_test_paths=(),
                repeats=receipt.repeats,
                timeout_s=timeout_s,
                proof_python=proof_python,
                proof_base_args=proof_base_args,
                proof_cmd_str=proof_cmd_str,
                reg_python=reg_python,
                reg_base_args=reg_base_args,
                reg_cmd_str=reg_cmd_str,
                regression_paths=(),
                quarantined_node_ids=receipt.quarantined_node_ids,
                tmp_dir=tmp_dir,
                hmac_key=hmac_key,
                preset_proof_node_ids=receipt.proof_node_ids,
                preset_regression_node_ids=receipt.regression_node_ids,
            )
        finally:
            for wt in (wt_arm3, wt_arm2, wt_arm1, wt_base):
                _remove_worktree(repo_root, wt)
