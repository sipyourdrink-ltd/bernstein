"""Tests for the `bernstein lineage` v1 CLI subcommands (ADR-009)."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

from click.testing import CliRunner

from bernstein.cli.commands.lineage_cmd import lineage_cmd
from bernstein.core.lineage.entry import LineageEntry, canonicalise, entry_hash
from bernstein.core.lineage.identity import generate_keypair, sign_detached


def _h(seed: str) -> str:
    return "sha256:" + (seed * 64)[:64]


def _mk_entry(
    agent_id: str,
    kid: str,
    artefact_path: str,
    content_hash: str,
    parent_hashes: list[str],
    ts_ns: int,
) -> LineageEntry:
    return LineageEntry(
        v=1,
        artefact_path=artefact_path,
        artefact_kind="file",
        content_hash=content_hash,
        parent_hashes=parent_hashes,
        agent_id=agent_id,
        agent_card_kid=kid,
        tool_call_id="tc",
        span_id="span",
        ts_ns=ts_ns,
        operator_hmac="deadbeef" * 8,
    )


def _write_setup(tmp_path: Path, entries: list[LineageEntry]) -> tuple[Path, Path, str, str]:
    log = tmp_path / "lineage" / "log.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    cards = tmp_path / "agents"
    priv, pub = generate_keypair()
    card_dir = cards / "agent:a"
    card_dir.mkdir(parents=True, exist_ok=True)
    (card_dir / "card.json").write_text(
        json.dumps(
            {
                "protocolVersion": "a2a/1.0",
                "agent_id": "agent:a",
                "kid": "k1",
                "public_key_pem": pub,
            }
        )
    )
    # Write the canonical JCS bytes the real ``LineageStore.append`` emits. The
    # gate binds verification to the on-disk bytes (issue #1848), so a faithful
    # fixture must match the canonical form rather than ``json.dumps`` defaults.
    with log.open("wb") as f:
        for e in entries:
            f.write(canonicalise(e) + b"\n")
    # Sidecar JWS files.
    sig_root = log.parent / "signatures"
    for e in entries:
        canonical = canonicalise(e)
        jws = sign_detached(canonical, priv, kid="k1")
        eh = entry_hash(e)
        path_hash = hashlib.sha256(e.artefact_path.encode()).hexdigest()
        d = sig_root / path_hash[:2] / path_hash
        d.mkdir(parents=True, exist_ok=True)
        (d / (eh.replace("sha256:", "") + ".jws")).write_text(jws)
    return log, cards, priv, pub


# ── gate ────────────────────────────────────────────────────────────────────


def test_gate_pass(tmp_path: Path) -> None:
    g = _mk_entry("agent:a", "k1", "x.py", _h("1"), [], 1)
    log, cards, *_ = _write_setup(tmp_path, [g])
    runner = CliRunner()
    result = runner.invoke(lineage_cmd, ["gate", "--log", str(log), "--cards", str(cards)], catch_exceptions=False)
    assert result.exit_code == 0
    assert "PASS" in result.output


def test_gate_fail_exit_code(tmp_path: Path) -> None:
    g = _mk_entry("agent:a", "k1", "x.py", _h("1"), [], 1)
    f1 = _mk_entry("agent:a", "k1", "x.py", _h("2"), [entry_hash(g)], 2)
    f2 = _mk_entry("agent:a", "k1", "x.py", _h("3"), [entry_hash(g)], 3)
    log, cards, *_ = _write_setup(tmp_path, [g, f1, f2])
    runner = CliRunner()
    result = runner.invoke(lineage_cmd, ["gate", "--log", str(log), "--cards", str(cards)])
    assert result.exit_code == 1
    assert "FAIL" in result.output


def test_gate_skip_when_log_missing(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(lineage_cmd, ["gate", "--log", str(tmp_path / "nope.jsonl")])
    assert result.exit_code == 0
    assert "SKIP" in result.output


def test_gate_json_output(tmp_path: Path) -> None:
    g = _mk_entry("agent:a", "k1", "x.py", _h("1"), [], 1)
    log, cards, *_ = _write_setup(tmp_path, [g])
    runner = CliRunner()
    result = runner.invoke(
        lineage_cmd,
        ["gate", "--log", str(log), "--cards", str(cards), "--output-json"],
    )
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed["ok"] is True


# ── forks ───────────────────────────────────────────────────────────────────


def test_forks_none(tmp_path: Path) -> None:
    g = _mk_entry("agent:a", "k1", "x.py", _h("1"), [], 1)
    log, _, *_ = _write_setup(tmp_path, [g])
    runner = CliRunner()
    result = runner.invoke(lineage_cmd, ["forks", "--log", str(log)])
    assert result.exit_code == 0
    assert "No forks" in result.output


def test_forks_detected(tmp_path: Path) -> None:
    g = _mk_entry("agent:a", "k1", "x.py", _h("1"), [], 1)
    f1 = _mk_entry("agent:a", "k1", "x.py", _h("2"), [entry_hash(g)], 2)
    f2 = _mk_entry("agent:a", "k1", "x.py", _h("3"), [entry_hash(g)], 3)
    log, _, *_ = _write_setup(tmp_path, [g, f1, f2])
    runner = CliRunner()
    result = runner.invoke(lineage_cmd, ["forks", "--log", str(log), "--output-json"])
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert len(parsed) == 1
    assert parsed[0]["artefact_path"] == "x.py"


# ── chain ───────────────────────────────────────────────────────────────────


def test_chain_ok(tmp_path: Path) -> None:
    g = _mk_entry("agent:a", "k1", "x.py", _h("1"), [], 1)
    log, cards, *_ = _write_setup(tmp_path, [g])
    runner = CliRunner()
    result = runner.invoke(lineage_cmd, ["chain", "x.py", "--log", str(log), "--cards", str(cards)])
    assert result.exit_code == 0
    assert "chain OK" in result.output


def test_chain_unknown_artefact(tmp_path: Path) -> None:
    g = _mk_entry("agent:a", "k1", "x.py", _h("1"), [], 1)
    log, cards, *_ = _write_setup(tmp_path, [g])
    runner = CliRunner()
    result = runner.invoke(lineage_cmd, ["chain", "other.py", "--log", str(log), "--cards", str(cards)])
    assert result.exit_code == 1


# ── export-prov (issue #5039) ─────────────────────────────────────────────


def test_export_prov_json_embeds_content_hash(tmp_path: Path) -> None:
    root = _mk_entry("agent:a", "k1", "x.py", _h("1"), [], 1)
    child = _mk_entry("agent:a", "k1", "x.py", _h("2"), [entry_hash(root)], 2)
    log, _cards, *_ = _write_setup(tmp_path, [root, child])
    runner = CliRunner()
    result = runner.invoke(lineage_cmd, ["export-prov", "x.py", "--log", str(log)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert _h("2") in json.dumps(payload)
    assert _h("1") in json.dumps(payload)


def test_export_prov_turtle_contains_derivation(tmp_path: Path) -> None:
    root = _mk_entry("agent:a", "k1", "x.py", _h("1"), [], 1)
    child = _mk_entry("agent:a", "k1", "x.py", _h("2"), [entry_hash(root)], 2)
    log, _cards, *_ = _write_setup(tmp_path, [root, child])
    runner = CliRunner()
    result = runner.invoke(lineage_cmd, ["export-prov", "x.py", "--format", "turtle", "--log", str(log)])
    assert result.exit_code == 0, result.output
    assert "prov:wasDerivedFrom" in result.output


def test_export_prov_unknown_artefact_exits_nonzero(tmp_path: Path) -> None:
    g = _mk_entry("agent:a", "k1", "x.py", _h("1"), [], 1)
    log, _cards, *_ = _write_setup(tmp_path, [g])
    runner = CliRunner()
    result = runner.invoke(lineage_cmd, ["export-prov", "other.py", "--log", str(log)])
    assert result.exit_code == 1


def test_export_prov_unresolved_fork_exits_nonzero(tmp_path: Path) -> None:
    root = _mk_entry("agent:a", "k1", "x.py", _h("1"), [], 1)
    fork_a = _mk_entry("agent:a", "k1", "x.py", _h("2"), [entry_hash(root)], 2)
    fork_b = _mk_entry("agent:a", "k1", "x.py", _h("3"), [entry_hash(root)], 3)
    log, _cards, *_ = _write_setup(tmp_path, [root, fork_a, fork_b])
    runner = CliRunner()
    result = runner.invoke(lineage_cmd, ["export-prov", "x.py", "--log", str(log)])
    assert result.exit_code == 1


def test_export_prov_writes_output_file(tmp_path: Path) -> None:
    g = _mk_entry("agent:a", "k1", "x.py", _h("1"), [], 1)
    log, _cards, *_ = _write_setup(tmp_path, [g])
    out = tmp_path / "out.json"
    runner = CliRunner()
    result = runner.invoke(lineage_cmd, ["export-prov", "x.py", "--log", str(log), "--output", str(out)])
    assert result.exit_code == 0, result.output
    assert _h("1") in out.read_text()


# ── reindex ─────────────────────────────────────────────────────────────────


def test_reindex_creates_projections(tmp_path: Path) -> None:
    g = _mk_entry("agent:a", "k1", "x.py", _h("1"), [], 1)
    c = _mk_entry("agent:a", "k1", "x.py", _h("2"), [entry_hash(g)], 2)
    log, _, *_ = _write_setup(tmp_path, [g, c])
    runner = CliRunner()
    result = runner.invoke(lineage_cmd, ["reindex", "--log", str(log)])
    assert result.exit_code == 0
    digest = hashlib.sha256(b"x.py").hexdigest()
    proj = log.parent / "by-artefact" / digest[:2] / (digest + ".jsonl")
    tips = log.parent / "tips" / (digest + ".json")
    assert proj.exists()
    assert tips.exists()
    tips_data = json.loads(tips.read_text())
    assert tips_data["open"] == [entry_hash(c)]


def test_reindex_round_trip_after_deletion(tmp_path: Path) -> None:
    """ADR-009 §12.4 scenario 6."""
    g = _mk_entry("agent:a", "k1", "x.py", _h("1"), [], 1)
    c = _mk_entry("agent:a", "k1", "x.py", _h("2"), [entry_hash(g)], 2)
    log, _, *_ = _write_setup(tmp_path, [g, c])
    runner = CliRunner()
    runner.invoke(lineage_cmd, ["reindex", "--log", str(log)])
    digest = hashlib.sha256(b"x.py").hexdigest()
    tips_a = (log.parent / "tips" / (digest + ".json")).read_text()
    # Delete + rebuild.
    import shutil

    shutil.rmtree(log.parent / "by-artefact", ignore_errors=True)
    shutil.rmtree(log.parent / "tips", ignore_errors=True)
    runner.invoke(lineage_cmd, ["reindex", "--log", str(log)])
    tips_b = (log.parent / "tips" / (digest + ".json")).read_text()
    assert tips_a == tips_b


# ── merge ───────────────────────────────────────────────────────────────────


def test_merge_rejects_unknown_winner(tmp_path: Path) -> None:
    g = _mk_entry("agent:a", "k1", "x.py", _h("1"), [], 1)
    f1 = _mk_entry("agent:a", "k1", "x.py", _h("2"), [entry_hash(g)], 2)
    f2 = _mk_entry("agent:a", "k1", "x.py", _h("3"), [entry_hash(g)], 3)
    log, _, *_ = _write_setup(tmp_path, [g, f1, f2])
    runner = CliRunner()
    result = runner.invoke(
        lineage_cmd,
        ["merge", "x.py", "--use-content", _h("999"), "--log", str(log)],
    )
    assert result.exit_code == 1


def test_merge_accepts_valid_winner(tmp_path: Path) -> None:
    g = _mk_entry("agent:a", "k1", "x.py", _h("1"), [], 1)
    f1 = _mk_entry("agent:a", "k1", "x.py", _h("2"), [entry_hash(g)], 2)
    f2 = _mk_entry("agent:a", "k1", "x.py", _h("3"), [entry_hash(g)], 3)
    log, _, *_ = _write_setup(tmp_path, [g, f1, f2])
    runner = CliRunner()
    result = runner.invoke(
        lineage_cmd,
        ["merge", "x.py", "--use-content", entry_hash(f1), "--log", str(log)],
    )
    assert result.exit_code == 0
    assert "Merge prepared" in result.output


# ── scripts/check_lineage.py ────────────────────────────────────────────────


def test_check_lineage_script_pass(tmp_path: Path) -> None:
    g = _mk_entry("agent:a", "k1", "x.py", _h("1"), [], 1)
    log, cards, *_ = _write_setup(tmp_path, [g])
    repo_root = Path(__file__).resolve().parents[3]
    script = repo_root / "scripts" / "check_lineage.py"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--log",
            str(log),
            "--cards",
            str(cards),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_check_lineage_script_fail(tmp_path: Path) -> None:
    g = _mk_entry("agent:a", "k1", "x.py", _h("1"), [], 1)
    f1 = _mk_entry("agent:a", "k1", "x.py", _h("2"), [entry_hash(g)], 2)
    f2 = _mk_entry("agent:a", "k1", "x.py", _h("3"), [entry_hash(g)], 3)
    log, cards, *_ = _write_setup(tmp_path, [g, f1, f2])
    repo_root = Path(__file__).resolve().parents[3]
    script = repo_root / "scripts" / "check_lineage.py"
    result = subprocess.run(
        [sys.executable, str(script), "--log", str(log), "--cards", str(cards)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1


def test_check_lineage_script_skip_no_log(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    script = repo_root / "scripts" / "check_lineage.py"
    result = subprocess.run(
        [sys.executable, str(script), "--log", str(tmp_path / "no.jsonl")],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "SKIP" in result.stdout
