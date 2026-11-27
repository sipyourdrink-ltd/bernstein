"""``resolve_session_journal_path`` and ACP journal projection refuse symlinked,
hardlinked, and non-regular session journals.

``contained_path`` follows links, so a ``journal.jsonl`` that points at a
file inside its own run directory still resolves inside the run directory.
Only the canonical-location check and the ``lstat`` regular-file / single-link
check in ``resolve_session_journal_path`` (and the matching ``O_NOFOLLOW`` +
``fstat`` check on the opened descriptor in ``project_journal_to_acp_updates``)
refuse those shapes.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from bernstein.core.protocols.acp.handlers import ACPRequestContext
from bernstein.core.protocols.acp.projection import project_journal_to_acp_updates
from bernstein.core.protocols.acp.schema import RESOURCE_NOT_FOUND, ACPSchemaError
from bernstein.core.protocols.acp.server import build_default_server
from bernstein.core.protocols.acp.session import resolve_session_journal_path
from bernstein.core.replay.journal import EventJournal

_RUN_ID = "run-1"


def _layout(tmp_path: Path) -> tuple[Path, Path]:
    sdd = tmp_path.resolve() / ".sdd"
    run_dir = sdd / "runs" / _RUN_ID
    run_dir.mkdir(parents=True)
    return sdd, run_dir


def _link_or_skip(link: Path, target: str | Path) -> None:
    """Create *link* -> *target*, or skip where the filesystem cannot.

    Unprivileged Windows runners cannot create symlinks, and a
    case-insensitive filesystem cannot hold a case-variant sibling.
    """
    try:
        link.symlink_to(target)
    except OSError:  # pragma: no cover - platform dependent
        pytest.skip("cannot create this link on this filesystem")


def _write(path: Path) -> None:
    path.write_text('{"event": "x"}\n', encoding="utf-8")


def _sibling_relative(run_dir: Path) -> None:
    _write(run_dir / "other.jsonl")
    _link_or_skip(run_dir / "journal.jsonl", "other.jsonl")


def _sibling_absolute(run_dir: Path) -> None:
    _write(run_dir / "other.jsonl")
    _link_or_skip(run_dir / "journal.jsonl", run_dir / "other.jsonl")


def _nested_journal(run_dir: Path) -> None:
    (run_dir / "sub").mkdir()
    _write(run_dir / "sub" / "journal.jsonl")
    _link_or_skip(run_dir / "journal.jsonl", Path("sub") / "journal.jsonl")


def _case_variant_sibling(run_dir: Path) -> None:
    _write(run_dir / "JOURNAL.jsonl")
    _link_or_skip(run_dir / "journal.jsonl", "JOURNAL.jsonl")


def _dangling(run_dir: Path) -> None:
    _link_or_skip(run_dir / "journal.jsonl", "missing.jsonl")


def _self_loop(run_dir: Path) -> None:
    _link_or_skip(run_dir / "journal.jsonl", "journal.jsonl")


def _two_link_loop(run_dir: Path) -> None:
    _link_or_skip(run_dir / "b.jsonl", "journal.jsonl")
    _link_or_skip(run_dir / "journal.jsonl", "b.jsonl")


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(_sibling_relative, id="sibling_relative"),
        pytest.param(_sibling_absolute, id="sibling_absolute"),
        pytest.param(_nested_journal, id="nested_journal"),
        pytest.param(_case_variant_sibling, id="case_variant_sibling"),
        pytest.param(_dangling, id="dangling"),
        pytest.param(_self_loop, id="self_loop"),
        pytest.param(_two_link_loop, id="two_link_loop"),
    ],
)
def test_symlinked_journal_inside_run_dir_is_refused(tmp_path: Path, build: Callable[[Path], None]) -> None:
    sdd, run_dir = _layout(tmp_path)
    build(run_dir)

    assert resolve_session_journal_path(sdd, _RUN_ID) == (None, False)


def test_regular_journal_is_accepted(tmp_path: Path) -> None:
    sdd, run_dir = _layout(tmp_path)
    _write(run_dir / "journal.jsonl")

    jpath, ok = resolve_session_journal_path(sdd, _RUN_ID)

    assert ok is True
    assert jpath == run_dir / "journal.jsonl"


def test_missing_journal_is_not_an_error(tmp_path: Path) -> None:
    sdd, _run_dir = _layout(tmp_path)

    assert resolve_session_journal_path(sdd, _RUN_ID) == (None, True)


def _hardlink_or_skip(target: Path, link: Path) -> None:
    """Create a hardlink *link* -> *target*, or skip where the filesystem cannot."""
    try:
        os.link(target, link)
    except OSError as exc:  # pragma: no cover - platform/filesystem dependent
        pytest.skip(f"cannot create hardlink on this filesystem: {exc}")


def _mkfifo_or_skip(path: Path) -> None:
    """Create a FIFO at *path*, or skip where the platform/filesystem cannot."""
    mkfifo = getattr(os, "mkfifo", None)
    if mkfifo is None:  # pragma: no cover - non-POSIX
        pytest.skip("os.mkfifo is not available on this platform")
    try:
        mkfifo(path)
    except OSError as exc:  # pragma: no cover - filesystem dependent
        pytest.skip(f"cannot create FIFO on this filesystem: {exc}")


def _call_with_timeout[T](fn: Callable[[], T], *, timeout: float = 2.0) -> T:
    """Run *fn* on a daemon thread so a blocking FIFO open fails fast instead of hanging."""
    box: dict[str, Any] = {}

    def _runner() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:
            box["exc"] = exc

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join(timeout=timeout)
    if thread.is_alive():
        pytest.fail(f"call timed out after {timeout}s (likely blocked opening a FIFO)")
    if "exc" in box:
        raise box["exc"]
    return box["value"]  # type: ignore[no-any-return]


def test_hardlinked_journal_to_sibling_run_is_refused(tmp_path: Path) -> None:
    sdd, run_dir = _layout(tmp_path)
    sibling_dir = sdd / "runs" / "cli-run-sibling"
    sibling_dir.mkdir(parents=True)
    sibling_journal = sibling_dir / "journal.jsonl"
    _write(sibling_journal)
    journal_path = run_dir / "journal.jsonl"
    _hardlink_or_skip(sibling_journal, journal_path)
    assert journal_path.lstat().st_nlink == 2

    assert resolve_session_journal_path(sdd, _RUN_ID) == (None, False)


def test_hardlinked_journal_inside_run_dir_is_refused(tmp_path: Path) -> None:
    sdd, run_dir = _layout(tmp_path)
    journal_path = run_dir / "journal.jsonl"
    _write(journal_path)
    _hardlink_or_skip(journal_path, run_dir / "journal-alias.jsonl")
    assert journal_path.lstat().st_nlink == 2

    assert resolve_session_journal_path(sdd, _RUN_ID) == (None, False)


def test_directory_named_journal_is_refused(tmp_path: Path) -> None:
    sdd, run_dir = _layout(tmp_path)
    (run_dir / "journal.jsonl").mkdir()

    assert resolve_session_journal_path(sdd, _RUN_ID) == (None, False)


def test_fifo_named_journal_is_refused_without_blocking(tmp_path: Path) -> None:
    sdd, run_dir = _layout(tmp_path)
    _mkfifo_or_skip(run_dir / "journal.jsonl")

    result = _call_with_timeout(lambda: resolve_session_journal_path(sdd, _RUN_ID))

    assert result == (None, False)


def test_project_journal_refuses_hardlinked_and_directory_targets(tmp_path: Path) -> None:
    sdd, run_dir = _layout(tmp_path)
    sibling = EventJournal("cli-run-sibling", sdd)
    sibling.record("agent_message", text="SIBLING_SECRET")

    hardlink = run_dir / "journal.jsonl"
    _hardlink_or_skip(sibling.path, hardlink)
    with pytest.raises(OSError):
        project_journal_to_acp_updates(hardlink)

    dir_target = run_dir / "dir_journal.jsonl"
    dir_target.mkdir()
    with pytest.raises(OSError):
        project_journal_to_acp_updates(dir_target)


def test_project_journal_refuses_symlink_on_posix(tmp_path: Path) -> None:
    if not hasattr(os, "O_NOFOLLOW"):  # pragma: no cover - non-POSIX
        pytest.skip("os.O_NOFOLLOW is not available on this platform")
    sdd, run_dir = _layout(tmp_path)
    sibling = EventJournal("cli-run-sibling", sdd)
    sibling.record("agent_message", text="SIBLING_SECRET")
    link_path = run_dir / "journal.jsonl"
    _link_or_skip(link_path, sibling.path)

    with pytest.raises(OSError):
        project_journal_to_acp_updates(link_path)


def test_project_journal_refuses_fifo_without_blocking(tmp_path: Path) -> None:
    _sdd, run_dir = _layout(tmp_path)
    fifo_path = run_dir / "journal.jsonl"
    _mkfifo_or_skip(fifo_path)

    with pytest.raises(OSError):
        _call_with_timeout(lambda: project_journal_to_acp_updates(fifo_path))


def test_session_load_rejects_hardlinked_sibling_journal(tmp_path: Path) -> None:
    """ACP session/load must refuse a hardlinked sibling journal without streaming its payload."""
    foreign = EventJournal("cli-run-sibling", tmp_path / ".sdd")
    foreign.record("agent_message", text="SIBLING_RUN_SECRET_PAYLOAD")

    frames: list[dict[str, Any]] = []

    async def _publish(frame: dict[str, Any]) -> None:
        frames.append(frame)

    server1 = build_default_server(server_url=None, adapter_name="mock", stream_publisher=_publish)
    sid_hardlink = ""

    async def _setup_and_check_inmem() -> None:
        nonlocal sid_hardlink
        await server1.registry.dispatch(ACPRequestContext("initialize", 1), {"protocolVersion": 1})
        created = await server1.registry.dispatch(
            ACPRequestContext("session/new", 2),
            {"cwd": str(tmp_path), "mcpServers": []},
        )
        sid_hardlink = created["sessionId"]

        run_dir = tmp_path / ".sdd" / "runs" / sid_hardlink
        run_dir.mkdir(parents=True, exist_ok=True)
        _hardlink_or_skip(foreign.path, run_dir / "journal.jsonl")

        with pytest.raises(ACPSchemaError) as inmem_exc:
            await server1.registry.dispatch(
                ACPRequestContext("session/load", 3),
                {"sessionId": sid_hardlink, "cwd": str(tmp_path), "mcpServers": []},
            )
        assert inmem_exc.value.code == RESOURCE_NOT_FOUND

    asyncio.run(_setup_and_check_inmem())

    server2 = build_default_server(server_url=None, adapter_name="mock", stream_publisher=_publish)

    async def _check_persisted() -> None:
        await server2.registry.dispatch(ACPRequestContext("initialize", 1), {"protocolVersion": 1})
        with pytest.raises(ACPSchemaError) as disk_exc:
            await server2.registry.dispatch(
                ACPRequestContext("session/load", 2),
                {"sessionId": sid_hardlink, "cwd": str(tmp_path), "mcpServers": []},
            )
        assert disk_exc.value.code == RESOURCE_NOT_FOUND

    asyncio.run(_check_persisted())
    assert "SIBLING_RUN_SECRET_PAYLOAD" not in json.dumps(frames)
