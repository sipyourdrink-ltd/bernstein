"""GET /tasks/next/{role} stamps the run's repository identity (#6538).

A remote worker refuses a claim addressed to a repository its workspace does
not hold; for that check to fire, the claim response must carry the central's
repository identity. ``next_task`` stamps ``repo_root_commit`` - the central
workspace's root commit - on the claim response; every other task route
leaves the field null, and a central without resolvable git history stamps
null rather than guessing.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from bernstein.core.bulletin import BulletinBoard, MessageBoard
from httpx import ASGITransport, AsyncClient

from bernstein.core.server import SSEBus, TaskStore, create_app

TASK_PAYLOAD = {
    "title": "Implement parser",
    "description": "Write the YAML parser module",
    "role": "backend",
}


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(("git", *args), cwd=repo, capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def _init_repo(path: Path) -> str:
    """Create a one-commit git repository and return its root commit."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "claim-identity@example.invalid")
    _git(path, "config", "user.name", "Claim Identity Test")
    (path / "app.py").write_text("print('seed')\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "seed")
    return _git(path, "rev-list", "--max-parents=0", "HEAD").splitlines()[0]


@pytest.fixture(scope="module")
def _module_app(tmp_path_factory: pytest.TempPathFactory):
    """Single FastAPI app shared across this module (create_app is expensive)."""
    jsonl_path = tmp_path_factory.mktemp("claim-identity") / "tasks.jsonl"
    return create_app(jsonl_path=jsonl_path)


@pytest.fixture()
def app(_module_app, tmp_path: Path):  # type: ignore[no-untyped-def]
    """Per-test fresh task state on the shared app instance."""
    _module_app.state.store = TaskStore(tmp_path / "tasks.jsonl")
    _module_app.state.bulletin = BulletinBoard()
    _module_app.state.message_board = MessageBoard()
    _module_app.state.sse_bus = SSEBus()
    _module_app.state.draining = False
    return _module_app


@pytest.fixture()
async def client(app) -> AsyncClient:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _point_workdir(app, path: Path) -> None:  # type: ignore[no-untyped-def]
    """Aim the app at *path* and drop any cached repository identity."""
    app.state.workdir = path
    if hasattr(app.state, "repo_root_commit"):
        del app.state.repo_root_commit


@pytest.mark.anyio
async def test_claim_response_stamps_the_central_repo_root_commit(app, client: AsyncClient, tmp_path: Path) -> None:
    repo = tmp_path / "central-repo"
    root = _init_repo(repo)
    _point_workdir(app, repo)
    await client.post("/tasks", json=TASK_PAYLOAD)

    resp = await client.get("/tasks/next/backend")

    assert resp.status_code == 200
    assert resp.json()["repo_root_commit"] == root


@pytest.mark.anyio
async def test_claim_response_identity_is_null_without_git_history(app, client: AsyncClient, tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    _point_workdir(app, plain)
    await client.post("/tasks", json=TASK_PAYLOAD)

    resp = await client.get("/tasks/next/backend")

    assert resp.status_code == 200
    assert resp.json()["repo_root_commit"] is None


@pytest.mark.anyio
async def test_other_task_routes_do_not_stamp_identity(app, client: AsyncClient, tmp_path: Path) -> None:
    repo = tmp_path / "central-repo"
    _init_repo(repo)
    _point_workdir(app, repo)
    created = await client.post("/tasks", json=TASK_PAYLOAD)
    task_id = created.json()["id"]

    resp = await client.get(f"/tasks/{task_id}")

    assert resp.status_code == 200
    assert resp.json()["repo_root_commit"] is None
