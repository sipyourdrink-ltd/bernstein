"""End-to-end CLI surface for named sandbox pools (#2547).

Exercises ``bernstein pool register / list / show / verify`` against a throwaway
working directory, proving the verbs are wired and that verify catches a
tampered pool body.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from bernstein.cli.commands.pool_cmd import pool_group

_SPEC = {
    "name": "ci-linux",
    "backend_allowlist": ["worktree", "docker"],
    "template": {"root": "/workspace", "env": {"FOO": "bar"}, "timeout_seconds": 900},
    "exposed_fields": ["env", "timeout_seconds"],
    "capability_ceiling": ["file_rw", "exec", "network"],
    "network_egress_class": "restricted",
    "credential_env_allowlist": ["AWS_ACCESS_KEY_ID"],
    "max_concurrency": 4,
}


def _write_spec(path: Path) -> Path:
    spec_file = path / "pool.json"
    spec_file.write_text(json.dumps(_SPEC), encoding="utf-8")
    return spec_file


def _run(args: list[str]):
    return CliRunner().invoke(pool_group, args)


class TestPoolCli:
    def test_register_list_show(self, tmp_path: Path):
        spec = _write_spec(tmp_path)
        res = _run(["register", str(spec), "--workdir", str(tmp_path)])
        assert res.exit_code == 0, res.output
        assert "registered" in res.output.lower()

        res = _run(["list", "--workdir", str(tmp_path), "--json"])
        assert res.exit_code == 0, res.output
        pools = json.loads(res.output)["pools"]
        assert "ci-linux" in pools

        res = _run(["show", "ci-linux", "--workdir", str(tmp_path)])
        assert res.exit_code == 0, res.output
        body = json.loads(res.output)
        assert body["name"] == "ci-linux"
        assert len(body["pool_hash"]) == 64

    def test_verify_passes_for_clean_store(self, tmp_path: Path):
        spec = _write_spec(tmp_path)
        _run(["register", str(spec), "--workdir", str(tmp_path)])
        res = _run(["verify", "--workdir", str(tmp_path)])
        assert res.exit_code == 0, res.output
        assert "passed" in res.output.lower()

    def test_verify_fails_on_tampered_body(self, tmp_path: Path):
        spec = _write_spec(tmp_path)
        _run(["register", str(spec), "--workdir", str(tmp_path)])
        pools_dir = tmp_path / ".sdd" / "sandbox" / "pools"
        body = next(pools_dir.glob("*.json"))
        text = body.read_text().replace('"timeout_seconds":900', '"timeout_seconds":1')
        assert '"timeout_seconds":1' in text
        body.write_text(text)
        res = _run(["verify", "--workdir", str(tmp_path)])
        assert res.exit_code == 1, res.output
        assert "failed" in res.output.lower()

    def test_reregister_same_spec_is_unchanged(self, tmp_path: Path):
        spec = _write_spec(tmp_path)
        _run(["register", str(spec), "--workdir", str(tmp_path)])
        res = _run(["register", str(spec), "--workdir", str(tmp_path)])
        assert res.exit_code == 0, res.output
        assert "unchanged" in res.output.lower()

    def test_update_changes_hash(self, tmp_path: Path):
        spec = _write_spec(tmp_path)
        _run(["register", str(spec), "--workdir", str(tmp_path)])
        updated = dict(_SPEC)
        updated["template"] = {"root": "/workspace", "env": {"FOO": "baz"}, "timeout_seconds": 1800}
        spec.write_text(json.dumps(updated), encoding="utf-8")
        res = _run(["register", str(spec), "--workdir", str(tmp_path)])
        assert res.exit_code == 0, res.output
        assert "updated" in res.output.lower()
