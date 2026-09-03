"""``bernstein cluster govern-inventory``: the operator surface for #4988.

13. test_cli_lists_every_workload_and_leaves_the_listing_file_untouched
14. test_cli_reports_leaving_governance_against_a_previous_inventory
15. test_cli_rejects_a_label_value_that_is_not_a_declaration
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from click.testing import CliRunner

from bernstein.cli.commands.cluster_cmd import cluster_group

FIXTURE = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "cluster" / "agent-workloads.json"


def _write_listing(tmp_path: Path, items: list[dict[str, Any]]) -> Path:
    path = tmp_path / "workloads.json"
    path.write_text(json.dumps({"kind": "List", "items": items}), encoding="utf-8")
    return path


def _fixture_items() -> list[dict[str, Any]]:
    return list(json.loads(FIXTURE.read_text(encoding="utf-8"))["items"])


def test_cli_lists_every_workload_and_leaves_the_listing_file_untouched(tmp_path: Path) -> None:
    listing = _write_listing(tmp_path, _fixture_items())
    before = listing.read_bytes()

    result = CliRunner().invoke(cluster_group, ["govern-inventory", "--manifests", str(listing), "--json"])

    assert result.exit_code == 0, result.output
    document = json.loads(result.output)
    states = {w["name"]: w["state"] for w in document["workloads"]}
    assert states == {
        "batch-agent": "opted_out",
        "research-agent": "governed",
        "scratch-agent": "ungoverned",
    }
    assert document["inventory_hash"].startswith("sha256:")
    assert listing.read_bytes() == before


def test_cli_reports_leaving_governance_against_a_previous_inventory(tmp_path: Path) -> None:
    runner = CliRunner()
    listing = _write_listing(tmp_path, _fixture_items())
    first = runner.invoke(cluster_group, ["govern-inventory", "--manifests", str(listing), "--json"])
    assert first.exit_code == 0, first.output
    previous = tmp_path / "previous.json"
    previous.write_text(first.output, encoding="utf-8")

    unlabelled = _fixture_items()
    del unlabelled[0]["metadata"]["labels"]["bernstein.io/govern"]
    later = _write_listing(tmp_path, unlabelled)

    result = runner.invoke(
        cluster_group,
        ["govern-inventory", "--manifests", str(later), "--previous", str(previous), "--json"],
    )

    assert result.exit_code == 0, result.output
    transitions = json.loads(result.output)["transitions"]
    assert transitions == [
        {
            "kind": "opted_out",
            "workload_ref": "agents/Deployment/research-agent",
            "uid": "3f2b1c40-0f3a-4a7e-9f2f-11d9c0a1b2c3",
            "previous_state": "governed",
            "current_state": "ungoverned",
        }
    ]


def test_cli_rejects_a_label_value_that_is_not_a_declaration(tmp_path: Path) -> None:
    items = _fixture_items()
    items[0]["metadata"]["labels"]["bernstein.io/govern"] = "enabledd"
    listing = _write_listing(tmp_path, items)

    result = CliRunner().invoke(cluster_group, ["govern-inventory", "--manifests", str(listing), "--json"])

    assert result.exit_code != 0
    assert "enabledd" in result.output
