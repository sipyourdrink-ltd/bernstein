"""Side-question ("btw") protocol for non-blocking agent queries.

Agents can post side questions without interrupting their main task flow.
Side queries use efficient cache-aligned parameters and are answered
through the manager LLM or from a knowledge base — never blocking the
orchestrator tick pipeline.

Intended use:
  from bernstein.side_query import SideQuery, post_side_query, get_side_answer

  # Agent posts a question
  post_side_query(agent_id, "What is the database migration strategy?")

  # Orchestrator/manager answers later
  get_side_answer(query_id)
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path  # noqa: TC003
from typing import Any

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class SideQuery:
    """A non-blocking question posted by an agent.

    Attributes:
        id: Unique query identifier (12-char hex).
        agent_id: Agent session that posted the question.
        task_id: Task the agent is working on.
        question: The question text.
        context: Optional context snippet (e.g. file paths, code excerpt).
        status: "open", "answered", "skipped".
        answer: Response text, if answered.
        created_at: Unix timestamp when posted.
        answered_at: Unix timestamp when answered, or 0.0.
    """

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    agent_id: str = ""
    task_id: str = ""
    question: str = ""
    context: str = ""
    status: str = "open"
    answer: str = ""
    created_at: float = field(default_factory=time.time)
    answered_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "agent_id": self.agent_id,
            "task_id": self.task_id,
            "question": self.question,
            "context": self.context,
            "status": self.status,
            "answer": self.answer,
            "created_at": self.created_at,
            "answered_at": self.answered_at,
        }
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SideQuery:
        return cls(
            id=str(d.get("id", "")),
            agent_id=str(d.get("agent_id", "")),
            task_id=str(d.get("task_id", "")),
            question=str(d.get("question", "")),
            context=str(d.get("context", "")),
            status=str(d.get("status", "open")),
            answer=str(d.get("answer", "")),
            created_at=float(d.get("created_at", 0.0) or 0.0),
            answered_at=float(d.get("answered_at", 0.0) or 0.0),
        )


# ---------------------------------------------------------------------------
# File-based store
# ---------------------------------------------------------------------------

_store_lock: Any = None  # lazily initialized threading.Lock


def _get_lock():
    global _store_lock
    if _store_lock is None:
        import threading

        _store_lock = threading.Lock()
    return _store_lock


def _store_path(store_dir: Path) -> Path:
    return store_dir / "side_queries.jsonl"


def post_side_query(
    store_dir: Path,
    agent_id: str,
    task_id: str,
    question: str,
    context: str = "",
) -> SideQuery:
    """Post a side query to the store.

    Args:
        store_dir: Directory to store side queries (usually .sdd/side_queries/).
        agent_id: Agent session identifier.
        task_id: Task the agent is working on.
        question: Question text.
        context: Optional context snippet.

    Returns:
        The posted SideQuery with its generated ID.
    """
    store_dir.mkdir(parents=True, exist_ok=True)
    query = SideQuery(agent_id=agent_id, task_id=task_id, question=question, context=context)
    with _get_lock():
        path = _store_path(store_dir)
        if path.exists():
            existing = _read_all(path)
            # Deduplicate: skip if same agent+task+question already posted
            for e in existing:
                if e.agent_id == agent_id and e.task_id == task_id and e.question == question and e.status != "skipped":
                    return e
        line = json.dumps(query.to_dict(), default=str)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    return query


def get_side_answer(store_dir: Path, query_id: str) -> SideQuery | None:
    """Find and return a side query by ID.

    Args:
        store_dir: Store directory.
        query_id: Query ID to look up.

    Returns:
        SideQuery instance or None if not found.
    """
    path = _store_path(store_dir)
    if not path.exists():
        return None
    with _get_lock():
        for q in _read_all(path):
            if q.id == query_id:
                return q
    return None


def get_open_queries(store_dir: Path) -> list[SideQuery]:
    """Return all open side queries.

    Args:
        store_dir: Store directory.

    Returns:
        List of open SideQuery instances.
    """
    path = _store_path(store_dir)
    if not path.exists():
        return []
    with _get_lock():
        return [q for q in _read_all(path) if q.status == "open"]


def answer_side_query(store_dir: Path, query_id: str, answer: str) -> bool:
    """Answer a side query.

    Appends a new line with the updated status to the JSONL store
    (JSONL append model — updates are new lines, not in-place).

    Args:
        store_dir: Store directory.
        query_id: Query ID to answer.
        answer: Answer text.

    Returns:
        True if the query was found and answered.
    """
    path = _store_path(store_dir)
    if not path.exists():
        return False
    with _get_lock():
        queries = _read_all(path)
        found = None
        for q in queries:
            if q.id == query_id and q.status == "open":
                found = q
                break
        if found is None:
            return False
        found.status = "answered"
        found.answer = answer
        found.answered_at = time.time()
        line = json.dumps(found.to_dict(), default=str)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    return True


def skip_side_query(store_dir: Path, query_id: str) -> bool:
    """Mark a side query as skipped.

    Args:
        store_dir: Store directory.
        query_id: Query ID to skip.

    Returns:
        True if the query was found and skipped.
    """
    path = _store_path(store_dir)
    if not path.exists():
        return False
    with _get_lock():
        queries = _read_all(path)
        found = None
        for q in queries:
            if q.id == query_id and q.status == "open":
                found = q
                break
        if found is None:
            return False
        found.status = "skipped"
        line = json.dumps(found.to_dict(), default=str)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    return True


def _read_all(path: Path) -> list[SideQuery]:
    """Read all entries and return the latest version of each query."""
    if not path.exists():
        return []
    queries: dict[str, SideQuery] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            q = SideQuery.from_dict(data)
            queries[q.id] = q
        except (json.JSONDecodeError, TypeError, KeyError):
            continue
    return list(queries.values())
