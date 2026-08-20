"""Unit tests for the storage-agnostic SOC 2 evidence pack exporter."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest

from bernstein.core.storage import (
    LocalFsSink,
    export_soc2_evidence_pack,
    register_sink,
)
from bernstein.core.storage import registry as storage_registry
from bernstein.core.storage import soc2_export as soc2_export_module
from bernstein.core.storage.sink import ArtifactSink, ArtifactStat, SinkError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


class _RecordingSink(ArtifactSink):
    """ArtifactSink test double that captures write calls."""

    name: str = "recorder"

    def __init__(self) -> None:
        self.written: dict[str, tuple[bytes, bool, str | None]] = {}

    async def write(
        self,
        key: str,
        data: bytes,
        *,
        durable: bool = True,
        content_type: str | None = None,
    ) -> None:
        self.written[key] = (data, durable, content_type)

    async def read(self, key: str) -> bytes:  # pragma: no cover
        if key not in self.written:
            raise FileNotFoundError(key)
        return self.written[key][0]

    async def list(self, prefix: str) -> list[str]:  # pragma: no cover
        return [k for k in sorted(self.written) if k.startswith(prefix)]

    async def delete(self, key: str) -> None:  # pragma: no cover
        self.written.pop(key, None)

    async def exists(self, key: str) -> bool:  # pragma: no cover
        return key in self.written

    async def stat(self, key: str) -> ArtifactStat:  # pragma: no cover
        if key not in self.written:
            raise FileNotFoundError(key)
        data, _, ct = self.written[key]
        return ArtifactStat(
            size_bytes=len(data),
            last_modified_unix=1234567890.0,
            content_type=ct,
        )

    async def close(self) -> None:  # pragma: no cover
        pass


class _FailingSink(ArtifactSink):
    """ArtifactSink test double whose write() always raises."""

    name: str = "failing"

    async def write(
        self,
        key: str,
        data: bytes,
        *,
        durable: bool = True,
        content_type: str | None = None,
    ) -> None:
        raise SinkError("simulated sink failure")

    async def read(self, key: str) -> bytes:  # pragma: no cover
        raise NotImplementedError

    async def list(self, prefix: str) -> list[str]:  # pragma: no cover
        raise NotImplementedError

    async def delete(self, key: str) -> None:  # pragma: no cover
        raise NotImplementedError

    async def exists(self, key: str) -> bool:  # pragma: no cover
        raise NotImplementedError

    async def stat(self, key: str) -> ArtifactStat:  # pragma: no cover
        raise NotImplementedError

    async def close(self) -> None:  # pragma: no cover
        raise NotImplementedError


@pytest.fixture(autouse=True)
def fresh_registry() -> Iterator[None]:
    """Isolate storage registry state per test."""
    storage_registry._reset_for_tests()
    yield
    storage_registry._reset_for_tests()


def test_export_noop_when_sink_name_is_none(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    md_path = tmp_path / "soc2-evidence-weekly.md"
    md_path.write_text("# SOC 2 Evidence", encoding="utf-8")
    manifest_path = tmp_path / "soc2-evidence-weekly.json"
    manifest_path.write_text('{"schema_version": 1}', encoding="utf-8")

    calls: list[str] = []
    monkeypatch.setattr(soc2_export_module, "get_sink", lambda name: calls.append(name))

    with caplog.at_level(logging.INFO):
        export_soc2_evidence_pack(
            md_path,
            manifest_path,
            sink_name=None,
            period_label="weekly",
            run_id="run-100",
        )

    assert "skipped: no sink_name" in caplog.text
    assert calls == [], "get_sink must never be called on the no-op path"


def test_export_noop_when_sink_name_is_empty(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    md_path = tmp_path / "soc2-evidence-weekly.md"
    md_path.write_text("# SOC 2 Evidence", encoding="utf-8")

    calls: list[str] = []
    monkeypatch.setattr(soc2_export_module, "get_sink", lambda name: calls.append(name))

    with caplog.at_level(logging.INFO):
        export_soc2_evidence_pack(
            md_path,
            None,
            sink_name="   ",
            period_label="weekly",
            run_id="run-100",
        )

    assert "skipped: no sink_name" in caplog.text
    assert calls == [], "get_sink must never be called on the no-op path"


def test_export_writes_to_registered_sink(tmp_path: Path) -> None:
    recorder = _RecordingSink()
    register_sink("recorder", recorder)

    src_dir = tmp_path / "src"
    src_dir.mkdir()
    md_file = src_dir / "soc2-evidence-2026-W34.md"
    md_file.write_text("# Checklist\n- [x] Control CC1.1", encoding="utf-8")
    json_file = src_dir / "soc2-evidence-2026-W34.json"
    json_file.write_text('{"period": "2026-W34", "count": 10}', encoding="utf-8")

    export_soc2_evidence_pack(
        md_file,
        json_file,
        sink_name="recorder",
        period_label="2026-W34",
        run_id="ci-9999",
    )

    expected_md_key = "soc2/2026-W34/ci-9999/soc2-evidence-2026-W34.md"
    expected_json_key = "soc2/2026-W34/ci-9999/soc2-evidence-2026-W34.json"

    assert expected_md_key in recorder.written
    assert expected_json_key in recorder.written

    md_data, md_durable, md_ct = recorder.written[expected_md_key]
    assert md_data == b"# Checklist\n- [x] Control CC1.1"
    assert md_durable is True
    assert md_ct == "text/markdown"

    json_data, json_durable, json_ct = recorder.written[expected_json_key]
    assert json_data == b'{"period": "2026-W34", "count": 10}'
    assert json_durable is True
    assert json_ct == "application/json"


def test_export_with_local_fs_sink(tmp_path: Path) -> None:
    sink_root = tmp_path / "sink_root"
    local_sink = LocalFsSink(sink_root)
    register_sink("fs_target", local_sink)

    md_file = tmp_path / "evidence.md"
    md_file.write_text("markdown content", encoding="utf-8")
    json_file = tmp_path / "evidence.json"
    json_file.write_text("{}", encoding="utf-8")

    export_soc2_evidence_pack(
        md_file,
        json_file,
        sink_name="fs_target",
        period_label="monthly",
        run_id="run-77",
    )

    dest_md = sink_root / "soc2" / "monthly" / "run-77" / "evidence.md"
    dest_json = sink_root / "soc2" / "monthly" / "run-77" / "evidence.json"

    assert dest_md.is_file()
    assert dest_json.is_file()
    assert dest_md.read_bytes() == md_file.read_bytes()
    assert dest_json.read_bytes() == json_file.read_bytes()


def test_export_handles_single_file_or_none(tmp_path: Path) -> None:
    recorder = _RecordingSink()
    register_sink("rec", recorder)

    md_file = tmp_path / "only_md.md"
    md_file.write_text("only md", encoding="utf-8")

    # Only markdown
    export_soc2_evidence_pack(
        md_file,
        None,
        sink_name="rec",
        period_label="q1",
        run_id="run-1",
    )
    assert "soc2/q1/run-1/only_md.md" in recorder.written
    assert len(recorder.written) == 1

    # Neither
    export_soc2_evidence_pack(
        None,
        None,
        sink_name="rec",
        period_label="q1",
        run_id="run-2",
    )
    assert len(recorder.written) == 1


@pytest.mark.asyncio
async def test_export_called_from_within_running_event_loop(tmp_path: Path) -> None:
    """Invoking the sync entry point from inside an active asyncio loop must succeed."""
    recorder = _RecordingSink()
    register_sink("rec_async", recorder)

    md_file = tmp_path / "in_loop.md"
    md_file.write_text("in loop", encoding="utf-8")

    export_soc2_evidence_pack(
        md_file,
        None,
        sink_name="rec_async",
        period_label="weekly",
        run_id="run-async",
    )

    assert "soc2/weekly/run-async/in_loop.md" in recorder.written


def test_export_unknown_sink_raises_keyerror(tmp_path: Path) -> None:
    md_file = tmp_path / "doc.md"
    md_file.write_text("doc", encoding="utf-8")

    with pytest.raises(KeyError, match="Unknown artifact sink"):
        export_soc2_evidence_pack(
            md_file,
            None,
            sink_name="nonexistent_sink_name",
            period_label="weekly",
            run_id="run-1",
        )


def test_export_propagates_sink_write_failure(tmp_path: Path) -> None:
    """A sink failure must surface to the caller, never be swallowed."""
    register_sink("failing", _FailingSink())

    md_file = tmp_path / "evidence.md"
    md_file.write_text("markdown content", encoding="utf-8")

    with pytest.raises(SinkError, match="simulated sink failure"):
        export_soc2_evidence_pack(
            md_file,
            None,
            sink_name="failing",
            period_label="weekly",
            run_id="run-1",
        )


def test_export_missing_file_raises_filenotfound(tmp_path: Path) -> None:
    recorder = _RecordingSink()
    register_sink("rec", recorder)

    missing_path = tmp_path / "does_not_exist.md"
    with pytest.raises(FileNotFoundError):
        export_soc2_evidence_pack(
            missing_path,
            None,
            sink_name="rec",
            period_label="weekly",
            run_id="run-1",
        )
