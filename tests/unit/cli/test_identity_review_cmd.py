"""CLI contracts for ``bernstein identity review`` (issue #4974).

The verbs are a surface over
:mod:`bernstein.core.identity.access_review`: no row is derived here and no
verdict is read from a field. ``verify`` fails when the reviewed bytes are not
the bytes some chain-anchored sign-off attached to.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.identity_cmd import identity_group
from bernstein.core.identity import access_review, delegation, grants

KEY = b"k" * 32
INSIDE = 1_700_100_000
SINCE = "2023-11-01"


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def audit_root(tmp_path, monkeypatch):
    monkeypatch.setattr(access_review, "_audit_key", lambda: KEY)
    signer = grants.GrantSigner.generate(issuer="manager:test")
    monkeypatch.setattr(access_review, "_install_signer", lambda: signer)
    grants.GrantLedger(root=tmp_path, key=KEY, signer=signer).issue_grant(
        run_id="run-a",
        task_id="t-1",
        secret_name="ANTHROPIC_API_KEY",
        audience="api.anthropic.com",
        capability_ceiling=("read",),
        grant_id="g-1",
        created=INSIDE,
    )
    delegation.DelegationLedger(root=tmp_path, key=KEY).record_hop(
        run_id="run-a",
        issuer="principal:alex",
        subject="orchestrator",
        audience="sub-agent:backend",
        act="task.spawn",
        created=INSIDE,
    )
    return tmp_path


class TestReviewShow:
    def test_review_emits_a_signed_document_over_the_windowed_chain(self, runner, audit_root):
        result = runner.invoke(
            identity_group,
            ["review", "--root", str(audit_root), "--since", SINCE],
        )
        assert result.exit_code == 0, result.output
        envelope = json.loads(result.output)
        assert envelope["review"]["schema"] == access_review.SCHEMA
        principals = {row["principal"] for row in envelope["review"]["rows"]}
        assert principals == {"task:t-1", "sub-agent:backend"}
        assert access_review.verify_signed_review(result.output.encode()).ok

    def test_csv_rendering_covers_the_same_rows_as_the_signed_document(self, runner, audit_root):
        signed = runner.invoke(identity_group, ["review", "--root", str(audit_root), "--since", SINCE])
        rendered = runner.invoke(
            identity_group,
            ["review", "--root", str(audit_root), "--since", SINCE, "--format", "csv"],
        )
        assert rendered.exit_code == 0, rendered.output
        rows = json.loads(signed.output)["review"]["rows"]
        body = rendered.output.splitlines()
        assert body[0].startswith("created,principal,event,authorized_by")
        assert len(body) == len(rows) + 1


class TestSignoffVerbs:
    def _write_review(self, runner, audit_root, tmp_path):
        doc = tmp_path / "review.json"
        result = runner.invoke(
            identity_group,
            ["review", "--root", str(audit_root), "--since", SINCE, "--output", str(doc)],
        )
        assert result.exit_code == 0, result.output
        return doc

    def test_verify_fails_before_any_signoff_exists(self, runner, audit_root, tmp_path):
        doc = self._write_review(runner, audit_root, tmp_path)
        result = runner.invoke(
            identity_group,
            ["review", "verify", "--root", str(audit_root), "--document", str(doc)],
        )
        assert result.exit_code == 1, result.output
        assert "no sign-off" in result.output.lower()

    def test_signoff_then_verify_names_the_reviewer(self, runner, audit_root, tmp_path):
        doc = self._write_review(runner, audit_root, tmp_path)
        signed_off = runner.invoke(
            identity_group,
            [
                "review",
                "sign-off",
                "--root",
                str(audit_root),
                "--document",
                str(doc),
                "--reviewer",
                "alex@example.com",
            ],
        )
        assert signed_off.exit_code == 0, signed_off.output
        result = runner.invoke(
            identity_group,
            ["review", "verify", "--root", str(audit_root), "--document", str(doc)],
        )
        assert result.exit_code == 0, result.output
        assert "alex@example.com" in result.output

    def test_verify_rejects_a_document_whose_bytes_were_edited_after_signoff(self, runner, audit_root, tmp_path):
        doc = self._write_review(runner, audit_root, tmp_path)
        runner.invoke(
            identity_group,
            [
                "review",
                "sign-off",
                "--root",
                str(audit_root),
                "--document",
                str(doc),
                "--reviewer",
                "alex@example.com",
            ],
        )
        envelope = json.loads(doc.read_text(encoding="utf-8"))
        envelope["review"]["rows"][0]["authorized_by"] = "manager:attacker"
        doc.write_text(json.dumps(envelope, sort_keys=True, indent=2) + "\n", encoding="utf-8")

        result = runner.invoke(
            identity_group,
            ["review", "verify", "--root", str(audit_root), "--document", str(doc)],
        )
        assert result.exit_code == 1, result.output
