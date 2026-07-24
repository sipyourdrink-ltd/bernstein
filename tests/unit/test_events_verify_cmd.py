"""``bernstein events verify`` boundary validation (#2653).

The CLI verifier must reject envelopes that a truncation attacker has stripped
of a fence-post or whose event list is not a homogeneous list of objects, before
handing them to the offline verifier. Otherwise dropping ``to_hmac`` and
truncating the tail passes verification unnoticed.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from bernstein.cli.commands.events_cmd import events_group
from bernstein.core.events.feed import project_window
from bernstein.core.security.audit_chain import AuditChainStore
from bernstein.core.security.audit_slice import slice_audit_log


def _valid_envelope(tmp_path: Path) -> dict[str, object]:
    audit_dir = tmp_path / "audit"
    chain = AuditChainStore(audit_dir, key=b"k" * 32)
    for i in range(3):
        chain.log_with_prev_digest(
            event_type="cost.update",
            actor="orchestrator",
            resource_type="run",
            resource_id=f"run_{i}",
            details={"run_id": f"run_{i}"},
        )
    window = project_window(slice_audit_log(audit_dir))
    return json.loads(window.to_envelope_json())


def _write(tmp_path: Path, envelope: object) -> Path:
    out = tmp_path / "window.json"
    out.write_text(json.dumps(envelope), encoding="utf-8")
    return out


def test_valid_window_verifies(tmp_path: Path) -> None:
    path = _write(tmp_path, _valid_envelope(tmp_path))
    result = CliRunner().invoke(events_group, ["verify", str(path)])
    assert result.exit_code == 0, result.output
    assert "verified" in result.output.lower()


def test_dropped_to_hmac_with_truncated_tail_is_rejected(tmp_path: Path) -> None:
    envelope = _valid_envelope(tmp_path)
    # Attacker drops the upper fence-post and truncates the last event; without
    # the boundary check the missing to_hmac disables the upper-bound check.
    envelope["events"] = envelope["events"][:-1]  # type: ignore[index]
    del envelope["to_hmac"]

    path = _write(tmp_path, envelope)
    result = CliRunner().invoke(events_group, ["verify", str(path)])
    assert result.exit_code == 1
    assert "feed envelope" in result.output.lower()


def test_dropped_from_hmac_is_rejected(tmp_path: Path) -> None:
    envelope = _valid_envelope(tmp_path)
    del envelope["from_hmac"]
    path = _write(tmp_path, envelope)
    result = CliRunner().invoke(events_group, ["verify", str(path)])
    assert result.exit_code == 1


def test_events_not_a_list_is_rejected(tmp_path: Path) -> None:
    envelope = _valid_envelope(tmp_path)
    envelope["events"] = {"not": "a list"}
    path = _write(tmp_path, envelope)
    result = CliRunner().invoke(events_group, ["verify", str(path)])
    assert result.exit_code == 1
    assert "list" in result.output.lower()


def test_non_dict_event_entry_is_rejected(tmp_path: Path) -> None:
    envelope = _valid_envelope(tmp_path)
    events = list(envelope["events"])  # type: ignore[arg-type]
    events.append("smuggled-scalar")
    envelope["events"] = events
    path = _write(tmp_path, envelope)
    result = CliRunner().invoke(events_group, ["verify", str(path)])
    assert result.exit_code == 1
