"""The README's demo recording ships with a signed run receipt; CI proves it.

``docs/assets/demo-run/`` carries the recording (``demo.cast`` /
``demo.gif``), the run receipt the *recorded run itself* produced, and
the Ed25519 public key that pins its signature. The README tells a
reader to verify the receipt offline; these tests run that exact
command on every push, so the published evidence cannot rot into a
decorative file (issue #3426): a receipt that stops verifying - or a
tampered copy that still looks official - fails the build. The negative
is demonstrated against a real mutation, not assumed.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from bernstein.cli.commands.verify_cmd import verify_cmd

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEMO_RUN = _REPO_ROOT / "docs" / "assets" / "demo-run"
_RECEIPT = _DEMO_RUN / "run-receipt.json"
_PUBKEY = _DEMO_RUN / "run-receipt.pub.pem"


def test_the_published_demo_receipt_verifies_offline_with_the_pinned_key() -> None:
    """The committed receipt must verify with provenance - the signature
    checked against the committed public key, not merely the key embedded
    in the file - because that is the exact command the README shows next
    to the recording.
    """
    result = CliRunner().invoke(verify_cmd, ["receipt", str(_RECEIPT), "--public-key", str(_PUBKEY)])
    assert result.exit_code == 0, result.output
    assert "provenance: pinned key" in result.output


def test_a_tampered_copy_of_the_published_receipt_fails_closed(tmp_path: Path) -> None:
    """Mutating one embedded journal row must flip the verdict to TAMPER
    DETECTED with exit 2 and a named divergent step. Without this
    demonstrated negative, the positive test could pass on a verifier
    that accepts anything.
    """
    doc = json.loads(_RECEIPT.read_text(encoding="utf-8"))
    doc["journal"]["events"][0]["forged-by-test"] = True
    tampered = tmp_path / "tampered-receipt.json"
    tampered.write_text(json.dumps(doc), encoding="utf-8")

    result = CliRunner().invoke(verify_cmd, ["receipt", str(tampered), "--public-key", str(_PUBKEY)])
    assert result.exit_code == 2, result.output
    assert "TAMPER DETECTED" in result.output
    assert "divergent journal step" in result.output


def test_the_recording_and_the_receipt_describe_the_same_run() -> None:
    """The committed cast ends on an on-camera verification whose header
    names the run and its journal event count. Those values must equal what
    the committed receipt declares - otherwise the cast and the receipt can
    be replaced independently and CI would bless a recording whose proof
    belongs to some other run (finding 3723934551).
    """
    import re

    cast_lines = (_DEMO_RUN / "demo.cast").read_text(encoding="utf-8").splitlines()
    events = [json.loads(line) for line in cast_lines[1:] if line.strip()]
    text = "".join(e[2] for e in events if isinstance(e, list) and len(e) >= 3 and e[1] == "o")
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)

    shown = re.search(r"run=(\S+)\s+journal_events=(\d+)", text)
    assert shown is not None, "the cast never shows the receipt verification header"

    receipt = json.loads(_RECEIPT.read_text(encoding="utf-8"))
    assert shown.group(1) == receipt["run_id"]
    assert int(shown.group(2)) == receipt["journal"]["event_count"]

    # The verdict the loop rests on must be the provenance-grade one.
    assert "OK (provenance: pinned key)" in text


def test_the_demo_receipt_has_a_non_empty_lineage_spine() -> None:
    """The published demo run receipt must carry a non-empty lineage spine
    (issue #3570).
    """
    receipt = json.loads(_RECEIPT.read_text(encoding="utf-8"))
    assert receipt["spine"]["entry_count"] > 0
    assert bool(receipt["spine"]["head_hash"])
