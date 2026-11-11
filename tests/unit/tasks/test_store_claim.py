"""Real-lock concurrency regressions for the TaskStore receipt adapter."""

# pyright: reportPrivateUsage=false
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import shutil
from pathlib import Path

import pytest

from bernstein.core.server.server_models import TaskCreate
from bernstein.core.tasks.claim import ClaimFilter
from bernstein.core.tasks.store_claim import claim_store_entry
from bernstein.core.tasks.task_store_core import TaskStore

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("scenario", ["valid", "invalid_manifest", "invalid_store", "identity"])
async def test_attach_manifest_preview(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str) -> None:
    from bernstein.core.admission.models import PoolSpec, Posture

    store = TaskStore(tmp_path / "tasks.jsonl")
    preview = getattr(store, "preview_attachment", None)
    initialize = getattr(store, "initialize_store_identity", None)
    assert callable(preview) and callable(initialize), "attachment primitives required"
    dep = await store.create(TaskCreate(id="dep", title="Dependency", description=""))
    for task_id in ("z-雪", "a"):
        await store.create(TaskCreate(id=task_id, title="Selected", description="", depends_on=[dep.id]))
    await store.add_progress("a", "Retained history", 10)
    await store.flush_buffer()
    identity = tmp_path / "tasks.jsonl.identity.json"
    store_id = await initialize()
    manifest = {
        "schema_version": "task-attach/v1",
        "run_id": "attach-" + "a" * 32,
        "store_id": store_id,
        "tenant_id": "default",
        "tasks": [{"task_id": t, "version": store.get_task(t).version} for t in ("z-雪", "a")],
        "limits": {
            "max_accepted_claims": 2,
            "max_concurrency": 1,
            "max_elapsed_seconds": 60,
            "resources": {"pool": "local", "max_slots": 0},
        },
        "derived_work": {"max_retries_per_task": 0},
    }
    pools = {"local": PoolSpec("local", 2, Posture.ENFORCE)}

    def files():
        return {str(p.relative_to(tmp_path)): p.read_bytes() if p.is_file() else None for p in tmp_path.rglob("*")}

    async def checked(raw, *, reject=False, selected_pools=None):
        before_files = files()
        before_state = copy.deepcopy({k: v for k, v in vars(store).items() if k != "_lock"})
        try:
            if reject:
                with pytest.raises(ValueError):
                    await preview(raw, pools if selected_pools is None else selected_pools)
            else:
                return await preview(raw, pools)
        finally:
            assert files() == before_files
            assert {k: v for k, v in vars(store).items() if k != "_lock"} == before_state

    def forbidden(*args, **kwargs):
        pytest.fail("preview attempted a mutation, grant, lease or process launch")

    if scenario == "identity":
        assert re.fullmatch(r"[a-f0-9]{32}", store_id)
        assert json.loads(identity.read_bytes()) == {"schema_version": "task-store-identity/v1", "store_id": store_id}
        before = files()
        assert await initialize() == store_id
        restarted = TaskStore(store._jsonl_path)
        restarted.replay_jsonl()
        assert restarted.read_store_identity() == store_id
        assert await restarted.initialize_store_identity() == store_id
        assert files() == before
        restored_path = tmp_path / "restored" / "tasks.jsonl"
        restored_path.parent.mkdir()
        shutil.copy2(store._jsonl_path, restored_path)
        shutil.copy2(identity, str(restored_path) + ".identity.json")
        restored = TaskStore(restored_path)
        restored.replay_jsonl()
        assert await restored.initialize_store_identity() == store_id
        assert (await restored.preview_attachment(json.dumps(manifest), pools)).tasks
        return

    for name in (
        "_append_jsonl",
        "_flush_buffer_unlocked",
        "_notify_task_updated",
        "_notify_task_created",
        "claim_next",
    ):
        monkeypatch.setattr(store, name, forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", forbidden)
    monkeypatch.setattr("subprocess.Popen", forbidden)
    if scenario == "valid":
        pending = None
        async with store._lock:
            pending = asyncio.create_task(preview(json.dumps(manifest), pools))
            await asyncio.sleep(0)
            assert not pending.done()
        await pending
        result = await checked(json.dumps(manifest, ensure_ascii=False))
        assert [(t.id, t.version) for t in result.tasks] == sorted(
            (t["task_id"], t["version"]) for t in manifest["tasks"]
        )
        canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        assert result.manifest_sha256 == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        reordered = json.loads(json.dumps(manifest), object_pairs_hook=lambda pairs: dict(reversed(pairs)))
        assert (await checked(json.dumps(reordered))).manifest_sha256 == result.manifest_sha256
        result.tasks[0].metadata["changed"] = True
        result.tasks[0].depends_on.clear()
        assert store.get_task("a").metadata == {} and store.get_task("a").depends_on == [dep.id]
        with pytest.raises(ValueError):
            TaskCreate(title="Administrative only", description="", run_id=manifest["run_id"])
        # The exact canonical comparison above covers every field, including fixed selectors.
        for path in [
            ("run_id",),
            ("limits", "max_accepted_claims"),
            ("limits", "max_concurrency"),
            ("limits", "max_elapsed_seconds"),
            ("limits", "resources", "max_slots"),
            ("derived_work", "max_retries_per_task"),
        ]:
            changed = copy.deepcopy(manifest)
            node = changed
            for key in path[:-1]:
                node = node[key]
            node[path[-1]] = "attach-" + "b" * 32 if path == ("run_id",) else node[path[-1]] + 1
            assert (await checked(json.dumps(changed))).manifest_sha256 != result.manifest_sha256
    elif scenario == "invalid_manifest":
        # Recursively test missing/unknown/duplicate keys and strict scalar types at every level.
        def invalid_nodes(node):
            if isinstance(node, (dict, list)):
                yield None
                yield "wrong container type"
            if isinstance(node, dict):
                yield {**node, "unknown": 1}
                for key, value in node.items():
                    yield {k: v for k, v in node.items() if k != key}
                    for replacement in invalid_nodes(value):
                        yield {**node, key: replacement}
            elif isinstance(node, list):
                yield []
                yield [*node, node[0]]
                for replacement in invalid_nodes(node[0]):
                    yield [replacement, *node[1:]]
            else:
                for value in (
                    None,
                    True,
                    False,
                    {},
                    [],
                    1.5,
                    "",
                    "bad/tenant",
                    "X" * 33,
                    "a" * 32 + "\n",
                    "attach-" + "a" * 32 + "\n",
                    "1",
                    -1,
                    float("inf"),
                    float("-inf"),
                    float("nan"),
                ):
                    if type(value) is not type(node) or value != node:
                        yield value
                if type(node) is int and node > 0:
                    yield 0

        for invalid in invalid_nodes(manifest):
            await checked(json.dumps(invalid), reject=True)
        for key in (
            "schema_version",
            "run_id",
            "store_id",
            "tenant_id",
            "tasks",
            "task_id",
            "version",
            "limits",
            "max_accepted_claims",
            "max_concurrency",
            "max_elapsed_seconds",
            "resources",
            "pool",
            "max_slots",
            "derived_work",
            "max_retries_per_task",
        ):
            raw = json.dumps(manifest)
            raw = raw.replace(json.dumps(key) + ":", json.dumps(key) + ": null, " + json.dumps(key) + ":", 1)
            await checked(raw, reject=True)
    elif scenario == "invalid_store":
        for path, value in [("task_id", "missing"), ("version", 999)]:
            changed = copy.deepcopy(manifest)
            changed["tasks"][0][path] = value
            await checked(json.dumps(changed), reject=True)
        store.get_task("a").tenant_id = "other"
        await checked(json.dumps(manifest), reject=True)
        store.get_task("a").tenant_id = "default"
        for spec in (
            None,
            object(),
            PoolSpec("local", 1, Posture.ADVISE),
            PoolSpec("local", 1, Posture.OFF),
            PoolSpec("local", -1),
            PoolSpec("local", True),
            PoolSpec("other", 1),
            PoolSpec("local", 1, "bogus"),
        ):
            await checked(json.dumps(manifest), reject=True, selected_pools={} if spec is None else {"local": spec})
        for raw in (
            "{}",
            '{"schema_version":"task-store-identity/v2","store_id":"' + store_id + '"}',
            '{"schema_version":"task-store-identity/v1","store_id":"bad"}',
            '{"schema_version":"task-store-identity/v1","store_id":null,"store_id":"' + store_id + '"}',
            json.dumps({"schema_version": "task-store-identity/v1", "store_id": store_id, "extra": 1}),
            "not json",
            '{"schema_version":"task-store-identity/v1","store_id":"' + "b" * 32 + '"}',
        ):
            identity.write_text(raw)
            await checked(json.dumps(manifest), reject=True)
        identity.unlink()
        await checked(json.dumps(manifest), reject=True)


async def test_competing_claims_acquire_same_task_only_once(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.jsonl")
    task = await store.create(TaskCreate(title="Brief", description="Internal brief", role="docs"))
    async with store._lock:
        claims = [
            asyncio.create_task(claim_store_entry(store, "default", ClaimFilter(role="docs"), worker))
            for worker in ("writer-1", "writer-2")
        ]
        await asyncio.sleep(0)
        assert all(not claim.done() for claim in claims)
    results = await asyncio.gather(*claims)
    granted = [entry for entry in results if entry is not None]
    assert len(granted) == 1
    assert granted[0].id == task.id
    assert task.status.value == "claimed"
    assert task.claimed_by_session in ("writer-1", "writer-2")


@pytest.mark.parametrize("has_next_task", [False, True])
async def test_candidate_rollback_while_waiting_for_lock_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, has_next_task: bool
) -> None:
    store = TaskStore(tmp_path / "tasks.jsonl")
    next_task = (
        await store.create(TaskCreate(title="Next", description="Next brief", role="docs", priority=3))
        if has_next_task
        else None
    )
    staged = asyncio.Event()
    release_flush = asyncio.Event()
    original_flush = store._flush_buffer_unlocked

    async def fail_gate_flush() -> None:
        staged.set()
        await release_flush.wait()
        raise OSError("Injected gate journal failure")

    monkeypatch.setattr(store, "_flush_buffer_unlocked", fail_gate_flush)
    gate = asyncio.create_task(
        store.create_gate_with_edges(
            clearance_task_id="vanishing-gate",
            title="Gate",
            role="docs",
            dependent_task_ids=[],
        )
    )
    await staged.wait()
    claiming = asyncio.create_task(claim_store_entry(store, "default", ClaimFilter(role="docs"), "writer"))
    await asyncio.sleep(0)
    assert store._lock.locked()
    assert not claiming.done()
    monkeypatch.setattr(store, "_flush_buffer_unlocked", original_flush)
    release_flush.set()
    with pytest.raises(OSError, match="Injected gate journal failure"):
        await gate
    entry = await claiming
    assert store.get_task("vanishing-gate") is None
    assert (entry.id if entry else None) == (next_task.id if next_task else None)
