"""Tests for `bernstein cache evict` and `bernstein cache policy` (issue #2551)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from click.testing import CliRunner

from bernstein.cli.main import cli
from bernstein.core.persistence.cache_eviction import (
    ServedFromEdge,
    open_ledger,
    open_tombstones,
)

if TYPE_CHECKING:
    from pathlib import Path


def _seed_served_from(workdir: Path) -> None:
    ledger = open_ledger(workdir)
    ledger.record(ServedFromEdge(cache_key="key_root", consumer="run_a"))
    ledger.record(ServedFromEdge(cache_key="key_root", consumer="key_child"))
    ledger.record(ServedFromEdge(cache_key="key_child", consumer="run_b"))


def test_cache_evict_json_reports_recall_set(tmp_path: Path) -> None:
    _seed_served_from(tmp_path)
    runner = CliRunner()

    result = runner.invoke(
        cli,
        ["cache", "evict", "key_root", "--reason", "pr_reverted", "--workdir", str(tmp_path), "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["root_key"] == "key_root"
    assert set(payload["tombstoned"]) == {"key_root", "key_child"}
    assert set(payload["consumers"]) == {"run_a", "run_b"}

    # The tombstone is durable and both keys are hard misses afterwards.
    tombstones = open_tombstones(tmp_path)
    assert tombstones.is_tombstoned("key_root")
    assert tombstones.is_tombstoned("key_child")


def test_cache_evict_emits_audit_event(tmp_path: Path) -> None:
    _seed_served_from(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["cache", "evict", "key_root", "--reason", "bad", "--workdir", str(tmp_path)],
        env={"BERNSTEIN_AUDIT_KEY_PATH": str(tmp_path / "audit.key")},
    )
    assert result.exit_code == 0, result.output

    from bernstein.core.security.audit_chain import EVENT_CACHE_EVICTION, AuditChainStore

    chain = AuditChainStore(tmp_path / ".sdd" / "audit", key=(tmp_path / "audit.key").read_bytes().strip())
    rows = chain.query(event_type=EVENT_CACHE_EVICTION)
    assert len(rows) == 1
    assert rows[0].details["cache_key"] == "key_root"
    assert rows[0].details["reason"] == "bad"


def test_cache_policy_show_json(tmp_path: Path) -> None:
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(
        json.dumps({"ingredients": ["task_inputs"], "expiry_mode": "drift", "drift_window": 2}),
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["cache", "policy", "--file", str(policy_file), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["policy"]["ingredients"] == ["task_inputs"]
    assert payload["policy_hash"].startswith("sha256:")
