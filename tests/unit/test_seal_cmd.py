"""CLI tests for ``bernstein seal publish`` / ``bernstein seal verify`` (#4205).

The commands anchor a run's sealed journal head to an RFC 3161 timestamp
token and check that anchor back offline. Two properties matter as much as
the crypto: nothing here reaches the network unless the operator names a TSA,
and no path reports a pass it did not actually verify.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from bernstein.cli.commands.seal_cmd import seal_group
from bernstein.core.replay.journal import EventJournal, seal_journal_into_spine
from bernstein.core.security.seal_anchor import ANCHOR_FILENAME

_SEAL_KEY = b"k" * 32
_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "rfc3161"
_TOKEN = _FIXTURE_DIR / "freetsa_token_with_certs.tsr"


def _sealed_journal(sdd_dir: Path, run_id: str) -> EventJournal:
    """Build a finalized journal and seal its head into the lineage spine.

    Writes the HMAC key where the autouse ``_isolate_audit_key`` fixture in
    ``tests/conftest.py`` already pointed ``BERNSTEIN_AUDIT_KEY_PATH``, so the
    read-only ``load_audit_key()`` the CLI uses resolves the same key.
    """
    key_path = Path(os.environ["BERNSTEIN_AUDIT_KEY_PATH"])
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(_SEAL_KEY)
    key_path.chmod(0o600)

    journal = EventJournal(run_id=run_id, sdd_dir=sdd_dir)
    journal.record("run_started", run_id=run_id)
    journal.record("task_completed", task_id="T-1")
    journal.record("run_completed", run_id=run_id)
    seal_journal_into_spine(
        journal,
        lineage_root=sdd_dir / "lineage",
        hmac_key=_SEAL_KEY,
        actor="orchestrator",
    )
    return journal


def _publish(sdd_dir: Path, run_id: str, *extra: str) -> Result:
    return CliRunner().invoke(
        seal_group,
        ["publish", run_id, "--sdd-dir", str(sdd_dir), "--token", str(_TOKEN), *extra],
    )


def test_publish_stores_the_anchor_next_to_the_run_journal(tmp_path: Path) -> None:
    """The anchor lands beside the journal it witnesses, pinned to that head."""
    sdd_dir = tmp_path / ".sdd"
    journal = _sealed_journal(sdd_dir, "run-pub")

    result = _publish(sdd_dir, "run-pub")

    assert result.exit_code == 0, result.output
    anchor_path = sdd_dir / "runs" / "run-pub" / ANCHOR_FILENAME
    assert anchor_path.is_file()
    record = json.loads(anchor_path.read_text(encoding="utf-8"))
    assert record["head_sha256"] == journal.head()
    assert record["anchor_kind"] == "rfc3161"
    assert record["run_id"] == "run-pub"


def test_publish_without_a_tsa_url_makes_no_network_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Anchoring is opt-in: with no TSA named, the command asks and stays offline."""
    import bernstein.core.security.seal_anchor as seal_anchor

    def _forbidden(*args: object, **kwargs: object) -> bytes:
        pytest.fail("seal publish reached the network without an explicit --tsa-url")

    monkeypatch.setattr(seal_anchor, "request_timestamp_token", _forbidden)

    sdd_dir = tmp_path / ".sdd"
    _sealed_journal(sdd_dir, "run-offline")

    result = CliRunner().invoke(seal_group, ["publish", "run-offline", "--sdd-dir", str(sdd_dir)])

    assert result.exit_code != 0
    assert "--tsa-url" in result.output
    assert not (sdd_dir / "runs" / "run-offline" / ANCHOR_FILENAME).exists()


def test_publish_refuses_a_journal_that_diverges_from_its_seal(tmp_path: Path) -> None:
    """A head that no longer matches the run's own seal is not anchorable."""
    sdd_dir = tmp_path / ".sdd"
    journal = _sealed_journal(sdd_dir, "run-diverged")
    lines = journal.path.read_text(encoding="utf-8").splitlines()
    journal.path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")

    result = _publish(sdd_dir, "run-diverged")

    assert result.exit_code != 0
    assert "sealed" in result.output.lower()
    assert not (sdd_dir / "runs" / "run-diverged" / ANCHOR_FILENAME).exists()


def test_verify_reports_mismatch_when_the_journal_moved_after_anchoring(tmp_path: Path) -> None:
    """Appending to an anchored journal is caught by the anchor, key or no key."""
    sdd_dir = tmp_path / ".sdd"
    journal = _sealed_journal(sdd_dir, "run-moved")
    assert _publish(sdd_dir, "run-moved").exit_code == 0

    journal.record("run_completed", run_id="run-moved")

    result = CliRunner().invoke(
        seal_group,
        [
            "verify",
            "run-moved",
            "--sdd-dir",
            str(sdd_dir),
            "--rfc3161-trusted-tsa-bundle",
            str(_FIXTURE_DIR / "freetsa_cacert.pem"),
        ],
    )

    assert result.exit_code != 0
    assert "mismatch" in result.output.lower()


def test_verify_without_a_trust_bundle_never_reports_a_pass(tmp_path: Path) -> None:
    """Unpinned TSA roots mean the verdict is unverifiable and the exit code is not 0."""
    sdd_dir = tmp_path / ".sdd"
    _sealed_journal(sdd_dir, "run-notrust")
    assert _publish(sdd_dir, "run-notrust").exit_code == 0

    result = CliRunner().invoke(
        seal_group,
        ["verify", "run-notrust", "--sdd-dir", str(sdd_dir), "--json"],
    )

    assert result.exit_code != 0
    assert json.loads(result.output)["status"] == "unverifiable"


def test_verify_without_an_anchor_says_so_rather_than_failing_open(tmp_path: Path) -> None:
    """A run that was never anchored is reported as such, not as verified."""
    sdd_dir = tmp_path / ".sdd"
    _sealed_journal(sdd_dir, "run-bare")

    result = CliRunner().invoke(seal_group, ["verify", "run-bare", "--sdd-dir", str(sdd_dir)])

    assert result.exit_code != 0
    assert "no anchor" in result.output.lower()


def _symlink_or_skip(link: Path, target: Path) -> None:
    """Point *link* at *target*, or skip where the platform forbids it."""
    try:
        link.symlink_to(target)
    except OSError:  # pragma: no cover - unprivileged Windows runners
        pytest.skip("cannot create symlinks on this platform")


def test_publish_refuses_a_run_whose_journal_is_a_symlink_out_of_the_runs_root(
    tmp_path: Path,
) -> None:
    """An ordinary run directory can still hold a journal that points away.

    Validating the run id as a safe segment does not cover this: the entry
    name is innocent and the directory really is under ``runs``. Only
    resolving the journal path itself catches it, and it has to be caught -
    anchoring here would mint a TSA-witnessed proof over a head that never
    belonged to this install.
    """
    sdd_dir = tmp_path / ".sdd"
    elsewhere = tmp_path / "elsewhere"
    outside = _sealed_journal(elsewhere, "run-outside")

    planted_dir = sdd_dir / "runs" / "run-planted"
    planted_dir.mkdir(parents=True)
    _symlink_or_skip(planted_dir / outside.path.name, outside.path)
    assert (planted_dir / outside.path.name).is_file()

    result = _publish(sdd_dir, "run-planted")

    assert result.exit_code != 0, result.output
    assert not (planted_dir / ANCHOR_FILENAME).exists()


def test_publish_refuses_to_write_an_anchor_through_a_symlink_out_of_the_runs_root(
    tmp_path: Path,
) -> None:
    """The anchor path is resolved before it is written, not just joined.

    ``write_anchor`` opens the path for writing, so a planted
    ``seal_anchor.json`` symlink turns ``publish`` into a write to whatever
    the link names. Resolving the path first is what keeps the write inside
    the runs root; the file outside has to come back untouched.
    """
    sdd_dir = tmp_path / ".sdd"
    _sealed_journal(sdd_dir, "run-clobber")

    outside = tmp_path / "not-ours.json"
    outside.write_text("keep me\n", encoding="utf-8")
    _symlink_or_skip(sdd_dir / "runs" / "run-clobber" / ANCHOR_FILENAME, outside)

    result = _publish(sdd_dir, "run-clobber")

    assert result.exit_code != 0, result.output
    assert outside.read_text(encoding="utf-8") == "keep me\n"


@pytest.mark.parametrize("hostile", ["../escape", "sub/dir", "..", "with space"])
def test_both_commands_refuse_a_run_id_that_is_not_a_safe_segment(tmp_path: Path, hostile: str) -> None:
    """A crafted run id names nothing, on either subcommand."""
    sdd_dir = tmp_path / ".sdd"
    _sealed_journal(sdd_dir, "run-real")

    assert _publish(sdd_dir, hostile).exit_code != 0
    assert CliRunner().invoke(seal_group, ["verify", hostile, "--sdd-dir", str(sdd_dir)]).exit_code != 0
