"""End-to-end CLI surface for reconciliation lanes (#5120).

Exercises ``bernstein govern lane bootstrap / list / show / retire`` against a
throwaway working directory, proving the verbs are wired to the chain and that
a repeat bootstrap against an unchanged set is a no-op that says so.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from bernstein.cli.commands.govern_lane_cmd import govern_lane_group

_SPEC = {
    "lanes": [
        {
            "name": "canary",
            "selector": "team:payments",
            "schedule": "*/15 * * * *",
            "log_destination": "s3://logs/lanes/canary",
            "timeout_seconds": 900,
            "barrier": "per-step",
        }
    ]
}


def _write_spec(path: Path, spec: dict) -> Path:
    spec_file = path / "lanes.json"
    spec_file.write_text(json.dumps(spec), encoding="utf-8")
    return spec_file


def _run(args: list[str]):
    return CliRunner().invoke(govern_lane_group, args)


class TestGovernLaneCli:
    def test_bootstrap_list_show(self, tmp_path: Path):
        spec = _write_spec(tmp_path, _SPEC)
        res = _run(["bootstrap", str(spec), "--workdir", str(tmp_path)])
        assert res.exit_code == 0, res.output
        assert "registered" in res.output.lower()

        res = _run(["list", "--workdir", str(tmp_path), "--json"])
        assert res.exit_code == 0, res.output
        lanes = json.loads(res.stdout)["lanes"]
        assert "canary" in lanes

        res = _run(["show", "canary", "--workdir", str(tmp_path)])
        assert res.exit_code == 0, res.output
        body = json.loads(res.stdout)
        assert body["name"] == "canary"
        assert len(body["lane_hash"]) == 64

    def test_json_output_carries_the_document_and_nothing_else(self, tmp_path: Path):
        spec = _write_spec(tmp_path, _SPEC)
        _run(["bootstrap", str(spec), "--workdir", str(tmp_path)])

        res = _run(["list", "--workdir", str(tmp_path), "--json"])

        assert res.exit_code == 0, res.output
        assert set(json.loads(res.stdout)) == {"lanes"}
        assert res.stdout.lstrip().startswith("{")

    def test_running_it_twice_against_an_unchanged_set_is_a_noop_and_says_so(self, tmp_path: Path):
        spec = _write_spec(tmp_path, _SPEC)
        _run(["bootstrap", str(spec), "--workdir", str(tmp_path)])
        res = _run(["bootstrap", str(spec), "--workdir", str(tmp_path), "--json"])
        assert res.exit_code == 0, res.output
        report = json.loads(res.stdout)
        assert report["noop"] is True
        assert report["changed"] == 0

        res = _run(["bootstrap", str(spec), "--workdir", str(tmp_path)])
        assert res.exit_code == 0, res.output
        assert "no-op" in res.output.lower()

    def test_update_changes_hash(self, tmp_path: Path):
        spec = _write_spec(tmp_path, _SPEC)
        _run(["bootstrap", str(spec), "--workdir", str(tmp_path)])
        updated = {"lanes": [{**_SPEC["lanes"][0], "timeout_seconds": 1800}]}
        spec.write_text(json.dumps(updated), encoding="utf-8")
        res = _run(["bootstrap", str(spec), "--workdir", str(tmp_path)])
        assert res.exit_code == 0, res.output
        assert "updated" in res.output.lower()

    def test_show_missing_lane_fails(self, tmp_path: Path):
        res = _run(["show", "nope", "--workdir", str(tmp_path)])
        assert res.exit_code == 1

    def test_invalid_spec_json_fails(self, tmp_path: Path):
        spec_file = tmp_path / "bad.json"
        spec_file.write_text("not json", encoding="utf-8")
        res = _run(["bootstrap", str(spec_file), "--workdir", str(tmp_path)])
        assert res.exit_code == 1

    def test_duplicate_lane_names_refused(self, tmp_path: Path):
        dupe = {"lanes": [_SPEC["lanes"][0], _SPEC["lanes"][0]]}
        spec = _write_spec(tmp_path, dupe)
        res = _run(["bootstrap", str(spec), "--workdir", str(tmp_path)])
        assert res.exit_code == 1


def _chain_length(workdir: Path) -> int:
    from bernstein.core.security.audit_chain import AuditChainStore

    return len(AuditChainStore(workdir / ".sdd" / "audit").query())


class TestGovernLaneRetire:
    def test_retired_lane_leaves_the_active_list(self, tmp_path: Path):
        spec = _write_spec(tmp_path, _SPEC)
        _run(["bootstrap", str(spec), "--workdir", str(tmp_path)])

        res = _run(["retire", "canary", "--workdir", str(tmp_path)])
        assert res.exit_code == 0, res.output
        assert "retired" in res.output.lower()

        res = _run(["list", "--workdir", str(tmp_path), "--json"])
        assert res.exit_code == 0, res.output
        assert "canary" not in json.loads(res.stdout)["lanes"]

    def test_json_output_names_the_lane_and_its_hash(self, tmp_path: Path):
        spec = _write_spec(tmp_path, _SPEC)
        _run(["bootstrap", str(spec), "--workdir", str(tmp_path)])
        active_hash = json.loads(_run(["list", "--workdir", str(tmp_path), "--json"]).stdout)["lanes"]["canary"]

        res = _run(["retire", "canary", "--workdir", str(tmp_path), "--json"])

        assert res.exit_code == 0, res.output
        assert res.stdout.lstrip().startswith("{")
        assert json.loads(res.stdout) == {"retired": "canary", "lane_hash": active_hash}

    def test_unknown_lane_fails_and_appends_nothing(self, tmp_path: Path):
        spec = _write_spec(tmp_path, _SPEC)
        _run(["bootstrap", str(spec), "--workdir", str(tmp_path)])
        before = _chain_length(tmp_path)

        res = _run(["retire", "nope", "--workdir", str(tmp_path)])

        assert res.exit_code == 1
        assert _chain_length(tmp_path) == before

    def test_retiring_twice_fails_and_appends_nothing(self, tmp_path: Path):
        spec = _write_spec(tmp_path, _SPEC)
        _run(["bootstrap", str(spec), "--workdir", str(tmp_path)])
        assert _run(["retire", "canary", "--workdir", str(tmp_path)]).exit_code == 0
        before = _chain_length(tmp_path)

        res = _run(["retire", "canary", "--workdir", str(tmp_path)])

        assert res.exit_code == 1
        assert _chain_length(tmp_path) == before
