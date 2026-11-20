"""Import-time regression tests for the ``bernstein.core.server`` package.

These guard against two coupled failure modes surfaced when a re-export in
``server.py`` referenced a symbol that ``server_middleware.py`` had stopped
defining:

* ``import bernstein.core.server.server`` raised ``ImportError`` outright
  (the stale ``_PUBLIC_PATH_PREFIXES`` re-export);
* ``from bernstein.core.server import <submodule>`` (e.g. ``server_launch``)
  failed *in isolation* because the package ``__getattr__`` fallback loop
  imports ``bernstein.core.server.server`` and only caught ``AttributeError``,
  leaking the unrelated ``ImportError`` for any attribute miss.

The subprocess tests use a *fresh* interpreter on purpose: once a module is
cached in ``sys.modules`` a repeated ``import`` is a no-op and would mask the
regression. Only a cold import re-executes ``server.py`` top-to-bottom.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

_SRC = str(Path(__file__).resolve().parents[2] / "src")


def _import_in_fresh_interpreter(statement: str) -> subprocess.CompletedProcess[str]:
    """Run *statement* in a cold interpreter and return the completed process."""
    script = f"import sys; sys.path.insert(0, {_SRC!r})\n{statement}\nprint('ok')"
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )


def test_import_server_module_in_fresh_interpreter() -> None:
    proc = _import_in_fresh_interpreter("import bernstein.core.server.server")
    assert proc.returncode == 0, "import bernstein.core.server.server failed:\n" + proc.stderr
    assert proc.stdout.strip() == "ok"


def test_from_server_import_submodule_in_fresh_interpreter() -> None:
    proc = _import_in_fresh_interpreter("from bernstein.core.server import server_launch")
    assert proc.returncode == 0, "from bernstein.core.server import server_launch failed:\n" + proc.stderr
    assert proc.stdout.strip() == "ok"


def test_getattr_tolerates_broken_fallback_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken fallback module must not corrupt unrelated attribute lookups.

    If any module in the ``__getattr__`` fallback loop fails to import, an
    attribute that is genuinely absent everywhere must still raise
    ``AttributeError`` - never leak the fallback module's ``ImportError``.
    """
    import bernstein.core.server as srv

    real_import_module = importlib.import_module

    def _fake_import_module(name: str, *args: object, **kwargs: object) -> object:
        if name == "bernstein.core.server.server":
            raise ImportError("simulated broken fallback")
        return real_import_module(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(importlib, "import_module", _fake_import_module)

    with pytest.raises(AttributeError):
        _ = srv.this_attribute_does_not_exist_anywhere


def test_bootstrap_does_not_load_web_application_into_parent_process() -> None:
    proc = _import_in_fresh_interpreter(
        "from bernstein.core.server import server_launch; assert 'bernstein.core.server.server_app' not in sys.modules"
    )
    assert proc.returncode == 0, proc.stderr


def test_task_server_handler_defers_editor_client_until_requested() -> None:
    script = """
import sys
from bernstein.core.acp import ACPHandler
from bernstein.core.protocols import acp
assert "bernstein.core.protocols.acp.client" not in sys.modules
assert "ACPEvent" in dir(acp)
from bernstein.core.protocols.acp import ACPEvent
from bernstein.core.protocols.acp.client import ACPEvent as concrete_event
assert ACPEvent is concrete_event
"""
    result = _import_in_fresh_interpreter(script)
    assert result.returncode == 0, result.stderr


def test_reading_current_branch_does_not_load_pull_request_helpers() -> None:
    script = """
import sys
from bernstein.core.git_ops import current_branch
assert "bernstein.core.git.git_pr" not in sys.modules
from bernstein.core.git_ops import create_github_pr
from bernstein.core.git.git_pr import create_github_pr as implementation
assert create_github_pr is implementation
"""
    result = _import_in_fresh_interpreter(script)
    assert result.returncode == 0, result.stderr


def test_registering_mission_routes_defers_digest_formatting() -> None:
    result = _import_in_fresh_interpreter(
        "from bernstein.core.routes.missions import router; "
        "assert 'bernstein.core.orchestration.mission_digest' not in sys.modules"
    )
    assert result.returncode == 0, result.stderr


def test_app_factory_defers_optional_worktree_and_hook_implementations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    for key in ("HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(key, str(tmp_path))
    result = _import_in_fresh_interpreter("""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bernstein.core.server.server_app import get_app
from bernstein.core.config.hook_protocol import validate_hook_payload
from bernstein.core.config.seed_parser import _parse_quality_gate_pipeline
from bernstein.core.routes.status_dashboard import _active_worktree_count

get_app()
assert _parse_quality_gate_pipeline([{"name": "lint"}])[0].name == "lint"
validate_hook_payload("undocumented", {})
project = Path.cwd()
sdd = project / ".sdd"
request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(sdd_dir=sdd, workdir=project)))
assert _active_worktree_count(request) == 0
assert "bernstein.core.quality.gate_runner" not in sys.modules
assert "bernstein.core.git.worktree" not in sys.modules
assert "jsonschema" not in sys.modules

validate_hook_payload("on_task_created", {"task_id": "a", "role": "backend", "title": "task"})
assert "jsonschema" in sys.modules
(sdd / "worktrees").mkdir(exist_ok=True, parents=True)
(project / ".git").mkdir(exist_ok=True)
with patch("bernstein.core.worktree.WorktreeManager.list_active", return_value=["one", "two"]):
    assert _active_worktree_count(request) == 2
""")
    assert result.returncode == 0, result.stderr
