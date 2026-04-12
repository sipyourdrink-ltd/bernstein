"""Sandboxed evaluation sessions for cloud-hosted Bernstein.

A sandbox session lets a prospect paste a public GitHub URL, pick a
solution pack, and watch up to 3 agents work within a $2 budget.
Sessions are ephemeral — cleaned up after completion or timeout.
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MAX_AGENTS = 3
MAX_BUDGET_USD = 2.0
SESSION_TIMEOUT_S = 1800  # 30 minutes
MAX_CONCURRENT_SESSIONS = 20
MAX_REPO_SIZE_MB = 200
CLEANUP_GRACE_S = 300  # keep workspace 5 min after finish for log viewing


class SessionStatus(StrEnum):
    QUEUED = "queued"
    CLONING = "cloning"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class SolutionPack(StrEnum):
    CODE_QUALITY = "code-quality"
    TEST_COVERAGE = "test-coverage"
    SECURITY_AUDIT = "security-audit"
    DOCUMENTATION = "documentation"
    PERFORMANCE = "performance"
    BUG_FIX = "bug-fix"
    DEPENDENCY_UPDATE = "dependency-update"
    REFACTOR = "refactor"


SOLUTION_PACK_CONFIGS: dict[SolutionPack, dict[str, Any]] = {
    SolutionPack.CODE_QUALITY: {
        "team": ["backend", "qa"],
        "goal": "Run linters, fix code style issues, and improve code quality across the repository",
        "max_agents": 2,
        "estimated_minutes": 10,
    },
    SolutionPack.TEST_COVERAGE: {
        "team": ["qa", "backend"],
        "goal": "Analyze test coverage gaps and write missing unit and integration tests",
        "max_agents": 3,
        "estimated_minutes": 15,
    },
    SolutionPack.SECURITY_AUDIT: {
        "team": ["security", "backend"],
        "goal": "Scan for OWASP Top 10 vulnerabilities, dependency CVEs, and hardcoded secrets",
        "max_agents": 2,
        "estimated_minutes": 10,
    },
    SolutionPack.DOCUMENTATION: {
        "team": ["docs", "architect"],
        "goal": "Generate README, API docs, architecture overview, and inline documentation",
        "max_agents": 2,
        "estimated_minutes": 10,
    },
    SolutionPack.PERFORMANCE: {
        "team": ["backend", "architect"],
        "goal": "Profile hot paths, identify N+1 queries, and optimize critical bottlenecks",
        "max_agents": 2,
        "estimated_minutes": 15,
    },
    SolutionPack.BUG_FIX: {
        "team": ["backend", "qa"],
        "goal": "Identify and fix open bugs from issue tracker or failing tests",
        "max_agents": 3,
        "estimated_minutes": 15,
    },
    SolutionPack.DEPENDENCY_UPDATE: {
        "team": ["backend", "qa"],
        "goal": "Update outdated dependencies, fix breaking changes, and verify tests pass",
        "max_agents": 2,
        "estimated_minutes": 10,
    },
    SolutionPack.REFACTOR: {
        "team": ["architect", "backend"],
        "goal": "Identify code smells, reduce duplication, and improve module structure",
        "max_agents": 2,
        "estimated_minutes": 15,
    },
}

_GITHUB_PUBLIC_RE = re.compile(r"^https://github\.com/[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+/?$")


@dataclass
class SandboxSession:
    """A single evaluation session with resource limits."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    repo_url: str = ""
    solution_pack: SolutionPack = SolutionPack.CODE_QUALITY
    status: SessionStatus = SessionStatus.QUEUED
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0
    budget_used_usd: float = 0.0
    agents_spawned: int = 0
    task_ids: list[str] = field(default_factory=list)
    error: str = ""
    workspace_path: str = ""
    client_ip_hash: str = ""

    @property
    def elapsed_s(self) -> float:
        if self.started_at == 0:
            return 0.0
        end = self.finished_at if self.finished_at > 0 else time.time()
        return end - self.started_at

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            SessionStatus.COMPLETED,
            SessionStatus.FAILED,
            SessionStatus.TIMED_OUT,
            SessionStatus.CANCELLED,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "repo_url": self.repo_url,
            "solution_pack": self.solution_pack.value,
            "status": self.status.value,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "budget_used_usd": round(self.budget_used_usd, 4),
            "budget_limit_usd": MAX_BUDGET_USD,
            "agents_spawned": self.agents_spawned,
            "max_agents": MAX_AGENTS,
            "task_ids": self.task_ids,
            "elapsed_s": round(self.elapsed_s, 1),
            "error": self.error,
        }


def validate_repo_url(url: str) -> str | None:
    """Return an error message if invalid, else None."""
    if not url or not url.strip():
        return "Repository URL is required"
    url = url.strip().rstrip("/")
    if not _GITHUB_PUBLIC_RE.match(url):
        return "Only public GitHub repository URLs are supported (https://github.com/owner/repo)"
    if ".." in url:
        return "Invalid repository URL"
    return None


def _workspace_dir(base: Path, session_id: str) -> Path:
    return base / "sandbox" / session_id


def _ip_hash(ip: str) -> str:
    return hashlib.sha256(ip.encode()).hexdigest()[:16]


class SandboxManager:
    """Manages sandbox evaluation sessions with resource limits."""

    def __init__(self, workspace_base: Path) -> None:
        self._base = workspace_base
        self._sessions: dict[str, SandboxSession] = {}
        self._ip_sessions: dict[str, list[str]] = {}
        self._max_per_ip = 3

    @property
    def active_count(self) -> int:
        return sum(
            1
            for s in self._sessions.values()
            if s.status in (SessionStatus.QUEUED, SessionStatus.CLONING, SessionStatus.RUNNING)
        )

    def create_session(
        self,
        repo_url: str,
        solution_pack: str,
        client_ip: str = "",
    ) -> SandboxSession:
        url_err = validate_repo_url(repo_url)
        if url_err:
            raise ValueError(url_err)

        try:
            pack = SolutionPack(solution_pack)
        except ValueError:
            valid = ", ".join(p.value for p in SolutionPack)
            raise ValueError(f"Unknown solution pack. Valid packs: {valid}") from None

        if self.active_count >= MAX_CONCURRENT_SESSIONS:
            raise RuntimeError("Too many active sessions — please try again later")

        ip_hash = _ip_hash(client_ip) if client_ip else ""
        if ip_hash:
            active_for_ip = [
                sid
                for sid in self._ip_sessions.get(ip_hash, [])
                if sid in self._sessions and not self._sessions[sid].is_terminal
            ]
            if len(active_for_ip) >= self._max_per_ip:
                raise RuntimeError(f"Maximum {self._max_per_ip} concurrent sessions per IP")

        session = SandboxSession(
            repo_url=repo_url.strip().rstrip("/"),
            solution_pack=pack,
            client_ip_hash=ip_hash,
        )
        session.workspace_path = str(_workspace_dir(self._base, session.id))
        self._sessions[session.id] = session

        if ip_hash:
            self._ip_sessions.setdefault(ip_hash, []).append(session.id)

        logger.info("Sandbox session created: %s repo=%s pack=%s", session.id, repo_url, pack.value)
        return session

    def get_session(self, session_id: str) -> SandboxSession | None:
        return self._sessions.get(session_id)

    def get_orchestrator_config(self, session: SandboxSession) -> dict[str, Any]:
        """Build bernstein.yaml-equivalent config for this sandbox session."""
        pack_cfg = SOLUTION_PACK_CONFIGS[session.solution_pack]
        return {
            "goal": pack_cfg["goal"],
            "cli": "auto",
            "team": pack_cfg["team"],
            "budget": f"${MAX_BUDGET_USD}",
            "max_agents": min(pack_cfg["max_agents"], MAX_AGENTS),
            "constraints": [
                "Public repo only — do not push, create PRs, or modify remote",
                "Read-only access to external services",
                f"Hard budget cap: ${MAX_BUDGET_USD}",
            ],
        }

    def record_cost(self, session_id: str, cost_usd: float) -> None:
        session = self._sessions.get(session_id)
        if not session:
            return
        session.budget_used_usd += cost_usd
        if session.budget_used_usd >= MAX_BUDGET_USD:
            self._finish(session, SessionStatus.COMPLETED, error="Budget exhausted")

    def mark_started(self, session_id: str) -> None:
        session = self._sessions.get(session_id)
        if session and session.status == SessionStatus.CLONING:
            session.status = SessionStatus.RUNNING
            session.started_at = time.time()

    def mark_cloning(self, session_id: str) -> None:
        session = self._sessions.get(session_id)
        if session and session.status == SessionStatus.QUEUED:
            session.status = SessionStatus.CLONING

    def finish(self, session_id: str, status: SessionStatus, error: str = "") -> None:
        session = self._sessions.get(session_id)
        if session:
            self._finish(session, status, error)

    def cancel(self, session_id: str) -> bool:
        session = self._sessions.get(session_id)
        if not session or session.is_terminal:
            return False
        self._finish(session, SessionStatus.CANCELLED)
        return True

    def check_timeouts(self) -> list[str]:
        """Return list of session IDs that just timed out."""
        timed_out: list[str] = []
        now = time.time()
        for session in self._sessions.values():
            if session.is_terminal:
                continue
            if session.created_at + SESSION_TIMEOUT_S < now:
                self._finish(session, SessionStatus.TIMED_OUT, error="Session timed out")
                timed_out.append(session.id)
        return timed_out

    def cleanup_finished(self) -> int:
        """Remove workspaces for sessions finished longer than grace period."""
        cleaned = 0
        now = time.time()
        for session in list(self._sessions.values()):
            if not session.is_terminal:
                continue
            if session.finished_at + CLEANUP_GRACE_S < now:
                ws = Path(session.workspace_path)
                if ws.exists():
                    shutil.rmtree(ws, ignore_errors=True)
                    cleaned += 1
                del self._sessions[session.id]
        return cleaned

    def list_sessions(self, include_finished: bool = False) -> list[dict[str, Any]]:
        return [
            s.to_dict()
            for s in sorted(self._sessions.values(), key=lambda s: s.created_at, reverse=True)
            if include_finished or not s.is_terminal
        ]

    def get_solution_packs(self) -> list[dict[str, Any]]:
        return [
            {
                "id": pack.value,
                "name": pack.value.replace("-", " ").title(),
                "goal": cfg["goal"],
                "team": cfg["team"],
                "max_agents": cfg["max_agents"],
                "estimated_minutes": cfg["estimated_minutes"],
            }
            for pack, cfg in SOLUTION_PACK_CONFIGS.items()
        ]

    def _finish(self, session: SandboxSession, status: SessionStatus, error: str = "") -> None:
        if session.is_terminal:
            return
        session.status = status
        session.finished_at = time.time()
        if error:
            session.error = error
        logger.info(
            "Sandbox session %s finished: status=%s elapsed=%.1fs cost=$%.4f",
            session.id,
            status.value,
            session.elapsed_s,
            session.budget_used_usd,
        )
