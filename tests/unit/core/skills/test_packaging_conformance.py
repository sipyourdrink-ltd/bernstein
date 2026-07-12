"""Unit tests for the packaged-skill conformance harness (issue #2369 tail).

The install path (#2412) and the update path (#2436) prove *what* content an
agent host is driving. The remaining issue ACs are the live-validation legs:

* "A fresh Claude Code session can install the plugin and launch a verified
  run end to end."
* "The skill works from at least 3 different agent CLIs against the same
  bernstein install."

The conformance harness proves both against one shared install by replaying
the skill's documented self-check command contract through a per-host
transport. The transport is the external boundary an agent CLI crosses when
it shells out ``bernstein ...``; production shells a subprocess, tests inject
a faithful in-process transport that runs the real CLI. The aggregate proof
is a content-addressed :class:`ConformanceReceipt` anchored in the ``skills``
lineage spine plus a ``plugin.conformance_receipt`` audit-chain event: strip
the chain and the pass/fail table is an untracked log; anchored, it is a
signed attestation that the skill drove N distinct hosts against one install.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from bernstein.cli.main import cli
from bernstein.core.security.audit_chain import (
    EVENT_PLUGIN_CONFORMANCE_RECEIPT,
    AuditChainStore,
)
from bernstein.core.skills.conformance import (
    CommandResult,
    ConformanceOutcome,
    HostConformanceResult,
    host_contract,
    run_conformance,
    run_host_conformance,
)
from bernstein.core.skills.packaging import (
    PACKAGED_SKILL_NAME,
    host_skill_parent,
    tree_content_hash,
)
from bernstein.core.skills.provenance import read_conformance_receipt

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

_KEY = b"0" * 32
_THREE_HOSTS = ("claude", "codex", "cursor")


def _bundled_source(tmp_path: Path) -> Path:
    """A small stand-in for the bundled skill tree (SKILL.md + a reference)."""
    src = tmp_path / "bundled" / PACKAGED_SKILL_NAME
    (src / "references").mkdir(parents=True, exist_ok=True)
    (src / "SKILL.md").write_text(
        "---\nname: bernstein-run\ndescription: conformance probe\n---\nbody\n",
        encoding="utf-8",
    )
    (src / "references" / "examples.md").write_text("example\n", encoding="utf-8")
    return src


class _InProcessTransport:
    """Faithful transport: run the real CLI in-process via ``CliRunner``.

    Only the OS-process boundary an agent CLI would cross is doubled; the exit
    codes are produced by bernstein's real ``skills package`` command code
    against the shared workdir, so a broken install is a real red verdict.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def invoke(self, host: str, argv: Sequence[str], *, cwd: Path) -> CommandResult:
        argv = tuple(argv)
        self.calls.append((host, argv))
        result = CliRunner().invoke(cli, list(argv))
        return CommandResult(argv=argv, exit_code=result.exit_code)


class _ScriptedTransport:
    """Return a caller-supplied exit code per host (for red-path tests)."""

    def __init__(self, exit_by_host: dict[str, int]) -> None:
        self._exit_by_host = exit_by_host

    def invoke(self, host: str, argv: Sequence[str], *, cwd: Path) -> CommandResult:
        return CommandResult(argv=tuple(argv), exit_code=self._exit_by_host.get(host, 0))


@pytest.fixture(autouse=True)
def _isolate_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    key_file = tmp_path / "audit.key"
    key_file.write_bytes(_KEY)
    key_file.chmod(0o600)
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(key_file))


def _workdir(tmp_path: Path) -> Path:
    workdir = tmp_path / "proj"
    workdir.mkdir(parents=True, exist_ok=True)
    return workdir


# ---------------------------------------------------------------------------
# host_contract shape
# ---------------------------------------------------------------------------


def test_host_contract_targets_workdir_and_skill_dir(tmp_path: Path) -> None:
    contract = host_contract(skill_dir=tmp_path / "skill", workdir=tmp_path / "proj")
    assert contract  # non-empty
    joined = [" ".join(cmd) for cmd in contract]
    assert any("verify" in c and str(tmp_path / "skill") in c for c in joined)
    # Every step passes the shared workdir so the probe is cwd-independent.
    assert all(str(tmp_path / "proj") in " ".join(cmd) for cmd in contract)


def test_contract_commands_documented_in_skill_md() -> None:
    """The probe only runs commands the shipped SKILL.md documents."""
    from bernstein.core.skills.packaging import packaged_skill_dir

    skill_body = (packaged_skill_dir() / "SKILL.md").read_text(encoding="utf-8")
    contract = host_contract(skill_dir=packaged_skill_dir(), workdir=packaged_skill_dir())
    for cmd in contract:
        # Drop runtime args (paths / flags), keep the documented verb prefix.
        prefix = " ".join(w for w in cmd if not w.startswith("-") and "/" not in w)
        assert f"bernstein {prefix}" in skill_body, f"{prefix!r} not documented in SKILL.md"


# ---------------------------------------------------------------------------
# run_host_conformance
# ---------------------------------------------------------------------------


def test_run_host_conformance_green_against_real_install(tmp_path: Path) -> None:
    workdir = _workdir(tmp_path)
    from bernstein.core.skills.packaging import install_packaged_skill

    dest = host_skill_parent("claude", "project", workdir=workdir) / PACKAGED_SKILL_NAME
    install_packaged_skill(
        workdir=workdir,
        dest=dest,
        source=_bundled_source(tmp_path),
        hmac_key=_KEY,
        install_id="agent-plugin-claude-project",
        timestamp=100,
        host="claude",
        scope="project",
    )
    result = run_host_conformance(
        host="claude",
        scope="project",
        workdir=workdir,
        skill_dir=dest,
        transport=_InProcessTransport(),
    )
    assert isinstance(result, HostConformanceResult)
    assert result.ok
    assert all(step.exit_code == 0 for step in result.steps)


def test_run_host_conformance_red_when_install_tampered(tmp_path: Path) -> None:
    """Faithful proof: tampering the installed tree flips the real verdict."""
    workdir = _workdir(tmp_path)
    from bernstein.core.skills.packaging import install_packaged_skill

    dest = host_skill_parent("claude", "project", workdir=workdir) / PACKAGED_SKILL_NAME
    install_packaged_skill(
        workdir=workdir,
        dest=dest,
        source=_bundled_source(tmp_path),
        hmac_key=_KEY,
        install_id="agent-plugin-claude-project",
        timestamp=100,
        host="claude",
        scope="project",
    )
    (dest / "SKILL.md").write_text("tampered\n", encoding="utf-8")

    result = run_host_conformance(
        host="claude",
        scope="project",
        workdir=workdir,
        skill_dir=dest,
        transport=_InProcessTransport(),
    )
    assert not result.ok
    assert any(step.exit_code != 0 for step in result.steps)


# ---------------------------------------------------------------------------
# run_conformance (aggregate)
# ---------------------------------------------------------------------------


def _run(tmp_path: Path, transport, *, hosts=_THREE_HOSTS, min_hosts=3, timestamp=100):
    return run_conformance(
        workdir=_workdir(tmp_path),
        hosts=hosts,
        transport=transport,
        hmac_key=_KEY,
        install_id="conformance",
        timestamp=timestamp,
        source=_bundled_source(tmp_path),
        min_hosts=min_hosts,
    )


def test_conformance_passes_across_three_hosts(tmp_path: Path) -> None:
    outcome = _run(tmp_path, _InProcessTransport())
    assert isinstance(outcome, ConformanceOutcome)
    assert outcome.ok
    assert set(outcome.passed_hosts) == set(_THREE_HOSTS)
    assert len(outcome.hosts) == 3
    assert outcome.skill_hash == tree_content_hash(_bundled_source(tmp_path))
    assert outcome.spine_anchor.startswith("sha256:")


def test_conformance_writes_readable_receipt(tmp_path: Path) -> None:
    workdir = _workdir(tmp_path)
    outcome = run_conformance(
        workdir=workdir,
        hosts=_THREE_HOSTS,
        transport=_InProcessTransport(),
        hmac_key=_KEY,
        install_id="conformance",
        timestamp=100,
        source=_bundled_source(tmp_path),
        min_hosts=3,
    )
    receipt = read_conformance_receipt(workdir, outcome.receipt_id)
    assert receipt is not None
    assert receipt.skill_hash == outcome.skill_hash
    assert receipt.min_hosts == 3
    assert dict(receipt.host_results) == dict.fromkeys(_THREE_HOSTS, True)


def test_conformance_records_audit_event(tmp_path: Path) -> None:
    workdir = _workdir(tmp_path)
    run_conformance(
        workdir=workdir,
        hosts=_THREE_HOSTS,
        transport=_InProcessTransport(),
        hmac_key=_KEY,
        install_id="conformance",
        timestamp=100,
        source=_bundled_source(tmp_path),
        min_hosts=3,
    )
    chain = AuditChainStore(workdir / ".sdd" / "audit", key=_KEY)
    events = chain.query(event_type=EVENT_PLUGIN_CONFORMANCE_RECEIPT)
    assert len(events) == 1
    assert events[0].details["ok"] is True
    assert events[0].details["passed_hosts"] == 3


def test_conformance_receipt_deterministic(tmp_path: Path) -> None:
    a = _run(tmp_path / "a", _InProcessTransport())
    b = _run(tmp_path / "b", _InProcessTransport())
    assert a.receipt_id == b.receipt_id


def test_conformance_below_min_hosts_not_ok(tmp_path: Path) -> None:
    """Two green hosts do not satisfy an AC that demands at least three."""
    outcome = _run(tmp_path, _InProcessTransport(), hosts=("claude", "codex"), min_hosts=3)
    assert not outcome.ok
    assert set(outcome.passed_hosts) == {"claude", "codex"}


def test_conformance_red_host_excluded_and_fails(tmp_path: Path) -> None:
    transport = _ScriptedTransport({"claude": 0, "codex": 0, "cursor": 2})
    outcome = _run(tmp_path, transport)
    assert not outcome.ok
    assert "cursor" not in outcome.passed_hosts
    assert set(outcome.passed_hosts) == {"claude", "codex"}
    receipt = read_conformance_receipt(_workdir(tmp_path), outcome.receipt_id)
    assert receipt is not None
    assert dict(receipt.host_results)["cursor"] is False
