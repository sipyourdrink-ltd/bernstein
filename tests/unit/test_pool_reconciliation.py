"""Tests for pool and limits pool domain separation and reconciliation (#3138).

Asserts that ``bernstein pool`` (sandbox pool manifests and audit chain projection)
and ``bernstein limits pool`` (admission slot pool ledger) maintain clear domain
separation, verifying that sandbox pool subcommands (register, list, show, verify)
and limits pool subcommands (create) execute and report against their respective
underlying stores without collision or cross-talk.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from click.testing import CliRunner

from bernstein.cli.commands.limits_cmd import (
    EXIT_OK as LIMITS_EXIT_OK,
)
from bernstein.cli.commands.limits_cmd import (
    EXIT_VERIFY_FAILED as LIMITS_EXIT_VERIFY_FAILED,
)
from bernstein.cli.commands.limits_cmd import (
    limits_group,
)
from bernstein.cli.commands.pool_cmd import pool_group
from bernstein.core.admission.engine import AdmissionEngine
from bernstein.core.admission.models import Posture

_SANDBOX_SPEC_A = {
    "name": "ci-sandbox",
    "backend_allowlist": ["worktree", "docker"],
    "template": {"root": "/workspace", "env": {"ENV_VAR": "a"}, "timeout_seconds": 600},
    "exposed_fields": ["env", "timeout_seconds"],
    "capability_ceiling": ["file_rw", "exec", "network"],
    "network_egress_class": "restricted",
    "credential_env_allowlist": ["API_KEY"],
    "max_concurrency": 4,
}

_SANDBOX_SPEC_SHARED = {
    "name": "shared-resource",
    "backend_allowlist": ["worktree"],
    "template": {"root": "/work", "env": {"TIER": "production"}, "timeout_seconds": 900},
    "exposed_fields": ["env"],
    "capability_ceiling": ["file_rw", "exec"],
    "network_egress_class": "none",
    "credential_env_allowlist": [],
    "max_concurrency": 2,
}


def _write_sandbox_spec(path: Path, spec: dict[str, Any], filename: str = "spec.json") -> Path:
    spec_file = path / filename
    spec_file.write_text(json.dumps(spec), encoding="utf-8")
    return spec_file


def _run_pool(args: list[str]) -> Any:
    return CliRunner().invoke(pool_group, args)


def _run_limits(args: list[str]) -> Any:
    return CliRunner().invoke(limits_group, args)


class TestPoolDomainSeparation:
    """Domain separation and isolation between sandbox pool and limits pool."""

    def test_sandbox_pool_registration_does_not_populate_limits_pool(self, tmp_path: Path) -> None:
        """Registering a sandbox pool stores it in sandbox store, not admission ledger."""
        spec_file = _write_sandbox_spec(tmp_path, _SANDBOX_SPEC_A)
        res = _run_pool(["register", str(spec_file), "--workdir", str(tmp_path)])
        assert res.exit_code == 0, res.output
        assert "registered" in res.output.lower()

        # Sandbox pool list and show report the pool
        list_res = _run_pool(["list", "--workdir", str(tmp_path), "--json"])
        assert list_res.exit_code == 0, list_res.output
        active_pools = json.loads(list_res.stdout)["pools"]
        assert "ci-sandbox" in active_pools

        show_res = _run_pool(["show", "ci-sandbox", "--workdir", str(tmp_path)])
        assert show_res.exit_code == 0, show_res.output
        manifest = json.loads(show_res.stdout)
        assert manifest["name"] == "ci-sandbox"
        assert manifest["max_concurrency"] == 4

        # Limits engine state does NOT see the sandbox pool as a limits pool
        engine = AdmissionEngine.for_workdir(tmp_path.resolve())
        assert "ci-sandbox" not in engine.state().pools

        # Limits status does not report the sandbox pool
        status_res = _run_limits(["status", "--workdir", str(tmp_path), "--json"])
        assert status_res.exit_code == LIMITS_EXIT_OK, status_res.output
        limits_state = json.loads(status_res.stdout)
        assert "ci-sandbox" not in limits_state.get("pools", {})

        # Both subsystems verify cleanly
        assert _run_pool(["verify", "--workdir", str(tmp_path)]).exit_code == 0
        assert _run_limits(["verify", "--workdir", str(tmp_path)]).exit_code == LIMITS_EXIT_OK

    def test_limits_pool_creation_does_not_populate_sandbox_pool(self, tmp_path: Path) -> None:
        """Creating a limits slot pool appends to admission ledger, not sandbox store."""
        create_res = _run_limits(
            ["pool", "create", "db-migration", "--slots", "1", "--posture", "enforce", "--workdir", str(tmp_path)]
        )
        assert create_res.exit_code == LIMITS_EXIT_OK, create_res.output

        # Limits state contains db-migration
        engine = AdmissionEngine.for_workdir(tmp_path.resolve())
        state = engine.state()
        assert "db-migration" in state.pools
        assert state.pools["db-migration"].slots == 1
        assert state.pools["db-migration"].posture == Posture.ENFORCE

        # Sandbox pool list is empty
        list_res = _run_pool(["list", "--workdir", str(tmp_path), "--json"])
        assert list_res.exit_code == 0, list_res.output
        active_pools = json.loads(list_res.stdout)["pools"]
        assert "db-migration" not in active_pools

        # Sandbox pool show for limits-only pool fails
        show_res = _run_pool(["show", "db-migration", "--workdir", str(tmp_path)])
        assert show_res.exit_code != 0

        # Both verifications pass
        assert _run_pool(["verify", "--workdir", str(tmp_path)]).exit_code == 0
        assert _run_limits(["verify", "--workdir", str(tmp_path)]).exit_code == LIMITS_EXIT_OK

    def test_same_name_pool_coexistence_without_conflict(self, tmp_path: Path) -> None:
        """A sandbox pool and a limits pool can share a name with distinct schema & behavior."""
        # 1. Register sandbox pool 'shared-resource'
        spec_file = _write_sandbox_spec(tmp_path, _SANDBOX_SPEC_SHARED)
        res_sb = _run_pool(["register", str(spec_file), "--workdir", str(tmp_path)])
        assert res_sb.exit_code == 0, res_sb.output

        # 2. Create limits pool 'shared-resource' with slots=5, posture=advise
        res_lim = _run_limits(
            ["pool", "create", "shared-resource", "--slots", "5", "--posture", "advise", "--workdir", str(tmp_path)]
        )
        assert res_lim.exit_code == LIMITS_EXIT_OK, res_lim.output

        # 3. Verify sandbox commands return sandbox manifest data
        list_res = _run_pool(["list", "--workdir", str(tmp_path), "--json"])
        assert list_res.exit_code == 0
        assert "shared-resource" in json.loads(list_res.stdout)["pools"]

        show_res = _run_pool(["show", "shared-resource", "--workdir", str(tmp_path)])
        assert show_res.exit_code == 0
        sb_body = json.loads(show_res.stdout)
        assert sb_body["name"] == "shared-resource"
        assert sb_body["backend_allowlist"] == ["worktree"]
        assert sb_body["max_concurrency"] == 2
        assert "slots" not in sb_body

        # 4. Verify limits status returns slot/posture data
        status_res = _run_limits(["status", "--workdir", str(tmp_path), "--json"])
        assert status_res.exit_code == LIMITS_EXIT_OK
        lim_state = json.loads(status_res.stdout)
        assert "shared-resource" in lim_state["pools"]
        assert lim_state["pools"]["shared-resource"]["slots"] == 5
        assert lim_state["pools"]["shared-resource"]["posture"] == "advise"
        assert "backend_allowlist" not in lim_state["pools"]["shared-resource"]

        # 5. Verify both verifications pass
        assert _run_pool(["verify", "--workdir", str(tmp_path)]).exit_code == 0
        assert _run_limits(["verify", "--workdir", str(tmp_path)]).exit_code == LIMITS_EXIT_OK

    def test_independent_mutation_on_shared_name_pool(self, tmp_path: Path) -> None:
        """Updating limits pool does not mutate sandbox pool, and vice versa."""
        # Initial registration of both
        spec_file = _write_sandbox_spec(tmp_path, _SANDBOX_SPEC_SHARED)
        _run_pool(["register", str(spec_file), "--workdir", str(tmp_path)])
        _run_limits(
            ["pool", "create", "shared-resource", "--slots", "2", "--posture", "enforce", "--workdir", str(tmp_path)]
        )

        initial_sb_manifest = json.loads(_run_pool(["show", "shared-resource", "--workdir", str(tmp_path)]).stdout)
        initial_sb_hash = initial_sb_manifest["pool_hash"]

        # Mutate limits pool: update slots to 10 and posture to off
        update_lim_res = _run_limits(
            ["pool", "create", "shared-resource", "--slots", "10", "--posture", "off", "--workdir", str(tmp_path)]
        )
        assert update_lim_res.exit_code == LIMITS_EXIT_OK

        # Sandbox pool is unchanged
        curr_sb_manifest = json.loads(_run_pool(["show", "shared-resource", "--workdir", str(tmp_path)]).stdout)
        assert curr_sb_manifest["pool_hash"] == initial_sb_hash
        assert curr_sb_manifest["max_concurrency"] == 2

        # Mutate sandbox pool: update max_concurrency to 8 and change env
        updated_spec = dict(_SANDBOX_SPEC_SHARED)
        updated_spec["max_concurrency"] = 8
        updated_spec["template"] = {"root": "/work", "env": {"TIER": "staging"}, "timeout_seconds": 1200}
        _write_sandbox_spec(tmp_path, updated_spec)
        update_sb_res = _run_pool(["register", str(spec_file), "--workdir", str(tmp_path)])
        assert update_sb_res.exit_code == 0
        assert "updated" in update_sb_res.output.lower()

        # Limits pool is unchanged
        lim_state = json.loads(_run_limits(["status", "--workdir", str(tmp_path), "--json"]).stdout)
        assert lim_state["pools"]["shared-resource"]["slots"] == 10
        assert lim_state["pools"]["shared-resource"]["posture"] == "off"

        # Sandbox pool reflects updated values
        after_sb_manifest = json.loads(_run_pool(["show", "shared-resource", "--workdir", str(tmp_path)]).stdout)
        assert after_sb_manifest["pool_hash"] != initial_sb_hash
        assert after_sb_manifest["max_concurrency"] == 8

    def test_tampering_isolation(self, tmp_path: Path) -> None:
        """Tampering with sandbox store breaks pool verify without affecting limits verify, and vice versa."""
        spec_file = _write_sandbox_spec(tmp_path, _SANDBOX_SPEC_A)
        _run_pool(["register", str(spec_file), "--workdir", str(tmp_path)])
        _run_limits(["pool", "create", "worker-limit", "--slots", "3", "--workdir", str(tmp_path)])

        # Verify initial clean state
        assert _run_pool(["verify", "--workdir", str(tmp_path)]).exit_code == 0
        assert _run_limits(["verify", "--workdir", str(tmp_path)]).exit_code == LIMITS_EXIT_OK

        # Tamper with sandbox pool json
        pools_dir = tmp_path / ".sdd" / "sandbox" / "pools"
        body_file = next(pools_dir.glob("*.json"))
        orig_content = body_file.read_text(encoding="utf-8")
        tampered_content = orig_content.replace('"timeout_seconds":600', '"timeout_seconds":9999')
        assert '"timeout_seconds":9999' in tampered_content
        body_file.write_text(tampered_content, encoding="utf-8")

        # Sandbox verify must FAIL, but limits verify must PASS
        pool_verify_res = _run_pool(["verify", "--workdir", str(tmp_path)])
        assert pool_verify_res.exit_code != 0
        assert "failed" in pool_verify_res.output.lower()

        limits_verify_res = _run_limits(["verify", "--workdir", str(tmp_path)])
        assert limits_verify_res.exit_code == LIMITS_EXIT_OK

        # Restore sandbox pool file
        body_file.write_text(orig_content, encoding="utf-8")
        assert _run_pool(["verify", "--workdir", str(tmp_path)]).exit_code == 0

        # Tamper with admission ledger
        ledger_file = tmp_path / ".sdd" / "admission" / "admission.ledger"
        if ledger_file.exists():
            data = bytearray(ledger_file.read_bytes())
            if len(data) > 10:
                data[-5] ^= 0xFF
                ledger_file.write_bytes(bytes(data))

                # Limits verify must FAIL, but sandbox verify must PASS
                assert _run_limits(["verify", "--workdir", str(tmp_path)]).exit_code == LIMITS_EXIT_VERIFY_FAILED
                assert _run_pool(["verify", "--workdir", str(tmp_path)]).exit_code == 0

    def test_json_output_mode_isolation(self, tmp_path: Path) -> None:
        """CLI --json outputs from both groups produce valid domain-isolated JSON."""
        # 1. Register sandbox pool with --json
        spec_file = _write_sandbox_spec(tmp_path, _SANDBOX_SPEC_A)
        res_reg = _run_pool(["register", str(spec_file), "--workdir", str(tmp_path), "--json"])
        assert res_reg.exit_code == 0
        reg_data = json.loads(res_reg.stdout)
        assert reg_data["action"] == "registered"
        assert reg_data["name"] == "ci-sandbox"
        assert "pool_hash" in reg_data

        # 2. Create limits pool with --json
        res_create = _run_limits(["pool", "create", "api-limit", "--slots", "5", "--workdir", str(tmp_path), "--json"])
        assert res_create.exit_code == LIMITS_EXIT_OK
        create_data = json.loads(res_create.stdout)
        assert create_data["name"] == "api-limit"
        assert create_data["slots"] == 5
        assert "entry_hash" in create_data

        # 3. List sandbox pools with --json
        res_list = _run_pool(["list", "--workdir", str(tmp_path), "--json"])
        assert res_list.exit_code == 0
        list_data = json.loads(res_list.stdout)
        assert set(list_data.keys()) == {"pools"}
        assert "ci-sandbox" in list_data["pools"]
        assert "api-limit" not in list_data["pools"]

        # 4. Limits status with --json
        res_status = _run_limits(["status", "--workdir", str(tmp_path), "--json"])
        assert res_status.exit_code == LIMITS_EXIT_OK
        status_data = json.loads(res_status.stdout)
        assert "api-limit" in status_data["pools"]
        assert "ci-sandbox" not in status_data["pools"]

        # 5. Limits verify with --json
        res_lim_verify = _run_limits(["verify", "--workdir", str(tmp_path), "--json"])
        assert res_lim_verify.exit_code == LIMITS_EXIT_OK
        verify_data = json.loads(res_lim_verify.stdout)
        assert verify_data["ok"] is True
        assert verify_data["errors"] == []

    def test_multiple_pools_reconciliation(self, tmp_path: Path) -> None:
        """Multiple distinct sandbox pools and limits pools operate cleanly in parallel."""
        # Create 2 sandbox specs
        spec1 = _write_sandbox_spec(tmp_path, _SANDBOX_SPEC_A, "spec1.json")
        spec2_dict = dict(_SANDBOX_SPEC_A)
        spec2_dict["name"] = "qa-sandbox"
        spec2_dict["max_concurrency"] = 2
        spec2 = _write_sandbox_spec(tmp_path, spec2_dict, "spec2.json")

        assert _run_pool(["register", str(spec1), "--workdir", str(tmp_path)]).exit_code == 0
        assert _run_pool(["register", str(spec2), "--workdir", str(tmp_path)]).exit_code == 0

        # Create 2 limits pools
        assert _run_limits(["pool", "create", "db-read", "--slots", "10", "--workdir", str(tmp_path)]).exit_code == 0
        assert _run_limits(["pool", "create", "db-write", "--slots", "1", "--workdir", str(tmp_path)]).exit_code == 0

        # Verify sandbox list only has the 2 sandbox pools
        sb_pools = json.loads(_run_pool(["list", "--workdir", str(tmp_path), "--json"]).stdout)["pools"]
        assert sorted(sb_pools.keys()) == ["ci-sandbox", "qa-sandbox"]

        # Verify limits status only has the 2 limits pools
        lim_pools = json.loads(_run_limits(["status", "--workdir", str(tmp_path), "--json"]).stdout)["pools"]
        assert sorted(lim_pools.keys()) == ["db-read", "db-write"]

        # Cross-lookups fail appropriately
        assert _run_pool(["show", "db-read", "--workdir", str(tmp_path)]).exit_code != 0
        assert _run_pool(["show", "db-write", "--workdir", str(tmp_path)]).exit_code != 0

        # Subsystem-level checks pass
        assert _run_pool(["verify", "--workdir", str(tmp_path)]).exit_code == 0
        assert _run_limits(["verify", "--workdir", str(tmp_path)]).exit_code == LIMITS_EXIT_OK
