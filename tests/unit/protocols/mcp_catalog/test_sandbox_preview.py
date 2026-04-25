"""Sandboxed dry-run install preview tests."""

from __future__ import annotations

import sys

from bernstein.core.protocols.mcp_catalog.manifest import CatalogEntry
from bernstein.core.protocols.mcp_catalog.sandbox_preview import (
    SandboxRunner,
    run_install_preview,
)


def _entry(install_command: list[str], *, version: str = "1.0.0") -> CatalogEntry:
    return CatalogEntry(
        id="fs-readonly",
        name="FS",
        description="x",
        homepage="https://x",
        repository="https://x.git",
        install_command=tuple(install_command),
        version_pin=version,
        transports=("stdio",),
        verified_by_bernstein=True,
    )


def test_preview_captures_stdout_stderr_and_exit_code() -> None:
    code = (
        "import sys; "
        "sys.stdout.write('out-line'); "
        "sys.stderr.write('err-line'); "
        "sys.exit(0)"
    )
    entry = _entry([sys.executable, "-c", code])
    preview = run_install_preview(entry, runner=SandboxRunner(timeout_seconds=10))
    assert preview.exit_code == 0
    assert preview.succeeded is True
    assert preview.stdout == b"out-line"
    assert preview.stderr == b"err-line"
    assert preview.timed_out is False


def test_preview_records_file_diff() -> None:
    code = (
        "from pathlib import Path; "
        "Path('hello.txt').write_text('hi'); "
        "Path('nested').mkdir(); "
        "Path('nested/x.txt').write_text('x')"
    )
    entry = _entry([sys.executable, "-c", code])
    preview = run_install_preview(entry)
    assert preview.exit_code == 0
    paths = sorted(d.path for d in preview.diff)
    assert paths == ["hello.txt", "nested/x.txt"]
    for change in preview.diff:
        assert change.change_type == "added"


def test_preview_handles_non_zero_exit() -> None:
    entry = _entry([sys.executable, "-c", "import sys; sys.exit(7)"])
    preview = run_install_preview(entry)
    assert preview.exit_code == 7
    assert preview.succeeded is False


def test_preview_handles_missing_executable() -> None:
    entry = _entry(["/no/such/binary-xyz123"])
    preview = run_install_preview(entry)
    assert preview.exit_code == 127
    assert b"command not found" in preview.stderr


def test_preview_respects_timeout() -> None:
    entry = _entry(
        [sys.executable, "-c", "import time; time.sleep(5)"],
    )
    runner = SandboxRunner(timeout_seconds=1)
    preview = run_install_preview(entry, runner=runner)
    assert preview.timed_out is True
    assert preview.succeeded is False
