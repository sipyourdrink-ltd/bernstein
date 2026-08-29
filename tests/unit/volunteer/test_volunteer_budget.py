"""Donor budget controls are persistent claim-time policy, not a daily quota."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from bernstein.adapters.capability_profile import iter_profiles
from bernstein.cli.commands.volunteer_cmd import volunteer_group
from bernstein.core.volunteer.budget import (
    BudgetLedger,
    BudgetReservation,
    VolunteerBudget,
    budget_line_items,
    complete_claim,
    filter_local_profiles,
    load_ledger,
    refuses_claim,
    remaining_wall_clock_minutes,
    reserve_claim,
    save_ledger,
)
from bernstein.core.volunteer.hub_app import build_hub_app
from bernstein.core.volunteer.lease_store import Lease, LeaseRefusal, LeaseStore
from bernstein.core.volunteer.manifest import load_manifest
from bernstein.core.volunteer.sandbox_profile import SandboxProfileRefusal, build_volunteer_profile


def test_task_refusal_on_exhausted_task_budget() -> None:
    budget = VolunteerBudget(max_tasks=1)
    ledger = BudgetLedger(tasks_used=1)

    refusal = refuses_claim(budget, ledger, task_size="size/s", token_estimate=0)

    assert refusal is not None
    assert refusal.reason == "task_budget_exhausted"
    assert "task" in refusal.detail


def test_a_partial_task_in_flight_completes_even_if_the_budget_is_exhausted_mid_run() -> None:
    budget = VolunteerBudget(max_tasks=1)
    in_flight = reserve_claim(
        budget,
        BudgetLedger(),
        claim_id="owner/repo#1",
        task_size="size/s",
        token_estimate=100,
    )

    assert refuses_claim(budget, in_flight, task_size="size/s", token_estimate=1) is not None

    completed = complete_claim(
        in_flight,
        claim_id="owner/repo#1",
        hours=0.5,
        actual_tokens=80,
    )
    assert completed.tasks_used == 1
    assert completed.hours_used == 0.5
    assert completed.tokens_used_actual == 80
    assert completed.reservations == ()


def test_in_flight_claim_reserves_wall_clock_for_the_next_claim() -> None:
    budget = VolunteerBudget(max_hours=2)
    ledger = reserve_claim(
        budget,
        BudgetLedger(),
        claim_id="first",
        task_size="s",
        token_estimate=0,
        wall_clock_hours=1.5,
    )

    refusal = refuses_claim(
        budget,
        ledger,
        task_size="s",
        token_estimate=0,
        wall_clock_hours=1,
    )

    assert refusal is not None
    assert refusal.reason == "wall_clock_budget_exhausted"
    assert remaining_wall_clock_minutes(budget, ledger) == 30


def test_remaining_hours_feed_the_existing_sandbox_profile_refusal() -> None:
    manifest = load_manifest(
        json.dumps(
            {
                "version": 1,
                "license": "Apache-2.0",
                "gates": [[sys.executable, "-c", "pass"]],
                "allowed_paths": ["src/**"],
                "egress_allowlist": [],
                "sandbox": "container",
                "max_wall_clock_minutes": 30,
                "local_ok": True,
            }
        )
    )

    with pytest.raises(SandboxProfileRefusal) as caught:
        build_volunteer_profile(
            manifest,
            available_backends={"container-userns"},
            donor_wall_clock_minutes=remaining_wall_clock_minutes(VolunteerBudget(max_hours=0), BudgetLedger()),
        )

    assert caught.value.reason == "wall_clock_below_floor"


def test_max_size_filters_offers() -> None:
    refusal = refuses_claim(
        VolunteerBudget(max_size="m"),
        BudgetLedger(),
        task_size="size/l",
        token_estimate=0,
    )

    assert refusal is not None
    assert refusal.reason == "size_cap_exceeded"
    assert "size/l" in refusal.detail


def test_local_only_excludes_api_key_adapters_using_registered_profiles() -> None:
    profiles = list(iter_profiles())
    assert any(profile.local_models for _, profile in profiles)
    assert any(not profile.local_models for _, profile in profiles)

    filtered = filter_local_profiles(VolunteerBudget(local_only=True), profiles)

    assert filtered
    assert all(profile.local_models for _, profile in filtered)
    assert len(filtered) < len(profiles)
    refusal = refuses_claim(
        VolunteerBudget(local_only=True),
        BudgetLedger(),
        task_size="s",
        token_estimate=0,
        adapter_profile=None,
    )
    assert refusal is not None
    assert refusal.reason == "local_only_adapter_required"


def test_ledger_survives_process_restart(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    ledger = BudgetLedger(
        tasks_used=2,
        hours_used=1.25,
        tokens_used_estimate=900,
        tokens_used_actual=850,
        reservations=(BudgetReservation("owner/repo#3", 100, 0.25),),
    )

    save_ledger(ledger, path)
    restarted_process = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json,sys; from pathlib import Path; "
                "from bernstein.core.volunteer.budget import load_ledger; "
                "print(json.dumps(load_ledger(Path(sys.argv[1])).to_dict(), sort_keys=True))"
            ),
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(restarted_process.stdout) == ledger.to_dict()


def test_token_estimate_is_reserved_at_claim_and_reconciled_at_completion() -> None:
    budget = VolunteerBudget(max_tokens=100)
    reserved = reserve_claim(
        budget,
        BudgetLedger(),
        claim_id="owner/repo#2",
        task_size="size/xs",
        token_estimate=80,
    )

    refusal = refuses_claim(budget, reserved, task_size="size/xs", token_estimate=30)
    assert refusal is not None
    assert refusal.reason == "token_budget_exhausted"

    reconciled = complete_claim(reserved, claim_id="owner/repo#2", hours=0.1, actual_tokens=50)
    assert reconciled.tokens_reserved == 0
    assert reconciled.tokens_used_estimate == 80
    assert reconciled.tokens_used_actual == 50
    assert refuses_claim(budget, reconciled, task_size="size/xs", token_estimate=50) is None


def test_receipt_contains_budget_line_items() -> None:
    budget = VolunteerBudget(max_tasks=4, max_hours=2.0, max_tokens=1_000)
    ledger = BudgetLedger(
        tasks_used=1,
        hours_used=0.5,
        tokens_used_estimate=400,
        tokens_used_actual=350,
    )

    items = budget_line_items(budget, ledger)

    assert [item["dimension"] for item in items] == ["tasks", "wall_clock", "tokens"]
    assert items[0] == {
        "dimension": "tasks",
        "unit": "tasks",
        "authorized": 4,
        "used": 1,
        "reserved": 0,
        "remaining": 3,
    }
    assert items[1]["unit"] == "hours"
    assert items[2]["reserved"] == 0
    assert items[2]["remaining"] == 650


def test_cli_flags_persist_the_budget_for_a_restart(tmp_path: Path) -> None:
    config = tmp_path / "budget.yaml"
    ledger = tmp_path / "ledger.json"
    runner = CliRunner()

    configured = runner.invoke(
        volunteer_group,
        [
            "budget",
            "--budget-tasks",
            "3",
            "--budget-hours",
            "1.5",
            "--budget-tokens",
            "2000",
            "--max-size",
            "m",
            "--local-only",
            "--config",
            str(config),
            "--ledger",
            str(ledger),
            "--json",
        ],
    )
    assert configured.exit_code == 0, configured.output

    restarted = runner.invoke(
        volunteer_group,
        ["budget", "--config", str(config), "--ledger", str(ledger), "--json"],
    )
    assert restarted.exit_code == 0, restarted.output
    assert restarted.output == configured.output


@pytest.mark.asyncio
async def test_lease_store_refuses_the_next_claim_and_reconciles_terminal_work(tmp_path: Path) -> None:
    ledger_path = tmp_path / "budget" / "ledger.json"
    store = LeaseStore(
        tmp_path / "leases.jsonl",
        budget=VolunteerBudget(max_tasks=1),
        budget_ledger_path=ledger_path,
    )
    first_worker = await store.enroll(Ed25519PrivateKey.generate().public_key())
    next_worker = await store.enroll(Ed25519PrivateKey.generate().public_key())

    first = await store.claim("task-1", first_worker, 300)
    refused = await store.claim("task-2", next_worker, 300)

    assert isinstance(first, Lease)
    assert isinstance(refused, LeaseRefusal)
    assert refused.reason.value == "task_budget_exhausted"
    assert await store.release("task-1", first_worker, actual_tokens=0) is None
    assert load_ledger(ledger_path).tasks_used == 1


def test_hub_projects_the_budget_dimension_in_its_claim_refusal(tmp_path: Path) -> None:
    store = LeaseStore(
        tmp_path / "leases.jsonl",
        budget=VolunteerBudget(max_tasks=0),
        budget_ledger_path=tmp_path / "budget.json",
    )
    client = TestClient(build_hub_app(store))
    public_key = Ed25519PrivateKey.generate().public_key()
    pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    worker_id = client.post("/volunteer/enroll", json={"public_key_pem": pem}).json()["worker_id"]

    refusal = client.post(
        "/volunteer/tasks/task-1/claim",
        json={"worker_id": worker_id, "ttl_seconds": 300},
    )

    assert refusal.status_code == 409
    assert "task budget" in refusal.json()["detail"]
