"""Tests for the per-ticket transcript bundle."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest
from bernstein.core.traces import TraceStep, TraceStore, new_trace

from bernstein.core.lineage.identity import AgentCard, generate_keypair
from bernstein.core.observability.ticket_bundle import (
    MANIFEST_SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    BundleManifest,
    BundleSelector,
    ManifestEntry,
    TicketBundle,
    default_selector,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """Populate a fake ``.sdd/`` layout for a ticket and return the root."""
    sdd = tmp_path / ".sdd"
    (sdd / "transcripts").mkdir(parents=True)
    (sdd / "traces").mkdir(parents=True)
    (sdd / "lineage").mkdir(parents=True)
    (sdd / "audit").mkdir(parents=True)
    (sdd / "pr").mkdir(parents=True)

    # Filename match -- includes ticket id verbatim
    (sdd / "transcripts" / "agent-backend-ENG-42.jsonl").write_text(
        '{"agent": "backend", "turn": 1, "text": "hello"}\n',
        encoding="utf-8",
    )
    (sdd / "traces" / "ENG-42-trace.jsonl").write_text(
        '{"event": "span", "ticket": "ENG-42"}\n',
        encoding="utf-8",
    )

    # Content match -- JSONL mentions tracker + ticket id but not in filename
    (sdd / "lineage" / "merge-audit.jsonl").write_text(
        '{"tracker": "github", "ticket_id": "ENG-42", "policy": "first-writer"}\n',
        encoding="utf-8",
    )
    (sdd / "audit" / "tracker-events.jsonl").write_text(
        '{"event": "ticket_opened", "tracker": "github", "ticket_id": "ENG-42"}\n',
        encoding="utf-8",
    )

    # PR payload -- must round-trip through manifest.pr_number
    (sdd / "pr" / "pr_77.json").write_text(
        json.dumps({"number": 77, "title": "ENG-42: fix bug", "ticket_id": "ENG-42"}),
        encoding="utf-8",
    )

    # Noise -- must NOT be picked up
    (sdd / "transcripts" / "agent-backend-OTHER-1.jsonl").write_text(
        '{"agent": "backend", "ticket_id": "OTHER-1"}\n',
        encoding="utf-8",
    )
    (sdd / "audit" / "unrelated.jsonl").write_text(
        '{"tracker": "jira", "ticket_id": "OTHER-1"}\n',
        encoding="utf-8",
    )
    return tmp_path


def _selector_without_git(workdir: Path) -> BundleSelector:
    """Default selector but with commits/PR readers that don't call git."""

    base = default_selector()

    def _no_commits(_w: Path, _t: str, _i: str) -> list[str]:
        return []

    # Keep PR payload as default (file-based); only commits use git.
    return BundleSelector(
        transcripts=base.transcripts,
        traces=base.traces,
        lineage=base.lineage,
        tracker_audit=base.tracker_audit,
        commits=_no_commits,
        pr_payload=base.pr_payload,
    )


# ---------------------------------------------------------------------------
# Assemble + manifest contents
# ---------------------------------------------------------------------------


def test_assemble_includes_filename_and_content_matches(workdir: Path) -> None:
    out = workdir / "ENG-42.tar.gz"
    bundle = TicketBundle(
        workdir=workdir,
        tracker="github",
        ticket_id="ENG-42",
        selector=_selector_without_git(workdir),
    )
    manifest = bundle.assemble(out=out)

    assert out.exists()
    arcnames = {entry.arcname for entry in manifest.files}
    # Filename matches
    assert "transcripts/agent-backend-ENG-42.jsonl" in arcnames
    assert "traces/ENG-42-trace.jsonl" in arcnames
    # Content matches (filename does not carry the ticket id)
    assert "lineage/merge-audit.jsonl" in arcnames
    assert "audit/tracker-events.jsonl" in arcnames
    # PR payload + git/commits.json
    assert "pr/pr_77.json" in arcnames
    assert "git/commits.json" in arcnames
    # Noise was excluded
    assert "transcripts/agent-backend-OTHER-1.jsonl" not in arcnames
    assert "audit/unrelated.jsonl" not in arcnames


def test_persisted_trace_secret_cannot_reappear_in_ticket_bundle(tmp_path: Path) -> None:
    canary = "gho_0123456789abcdefghijklmnopqrstuv"
    store = TraceStore(tmp_path / ".sdd" / "traces")
    trace = new_trace("sess-secret", ["ENG-42"], "backend", "sonnet", "high")
    trace.steps.append(
        TraceStep(type="verify", timestamp=1.0, detail=f"Authorization: Bearer {canary}")
    )
    store.write(trace)
    out = tmp_path / "ENG-42.tar.gz"
    bundle = TicketBundle(
        workdir=tmp_path,
        tracker="github",
        ticket_id="ENG-42",
        selector=_selector_without_git(tmp_path),
    )

    manifest = bundle.assemble(out=out)

    assert any(entry.arcname == "traces/ENG-42.jsonl" for entry in manifest.files)
    with tarfile.open(out, mode="r:*") as tf:
        trace_payload = tf.extractfile("traces/ENG-42.jsonl")
        assert trace_payload is not None
        persisted = trace_payload.read()
    assert canary.encode() not in persisted
    assert b"[REDACTED]" in persisted


def test_manifest_schema_and_metadata(workdir: Path) -> None:
    out = workdir / "ENG-42.tar.gz"
    bundle = TicketBundle(
        workdir=workdir,
        tracker="github",
        ticket_id="ENG-42",
        selector=_selector_without_git(workdir),
    )
    manifest = bundle.assemble(out=out)

    assert manifest.schema_version == MANIFEST_SCHEMA_VERSION
    assert manifest.tracker == "github"
    assert manifest.ticket_id == "ENG-42"
    assert manifest.pr_number == 77
    # Each manifest entry's recorded sha256 matches the actual SHA-256
    # of the bytes stored under that arcname (not just sha length).
    with tarfile.open(out, mode="r:*") as tf:
        for entry in manifest.files:
            member = tf.getmember(entry.arcname)
            fp = tf.extractfile(member)
            assert fp is not None
            data = fp.read()
            assert len(data) == entry.size_bytes
            assert hashlib.sha256(data).hexdigest() == entry.sha256


def test_manifest_inside_archive_matches_returned_manifest(workdir: Path) -> None:
    out = workdir / "ENG-42.tar.gz"
    bundle = TicketBundle(
        workdir=workdir,
        tracker="github",
        ticket_id="ENG-42",
        selector=_selector_without_git(workdir),
    )
    manifest = bundle.assemble(out=out)

    with tarfile.open(out, mode="r:*") as tf:
        fp = tf.extractfile(tf.getmember("manifest.json"))
        assert fp is not None
        on_disk = json.loads(fp.read())
    assert on_disk["tracker"] == "github"
    assert on_disk["ticket_id"] == "ENG-42"
    assert on_disk["schema_version"] == manifest.schema_version
    assert {f["arcname"] for f in on_disk["files"]} == {e.arcname for e in manifest.files}


def test_assemble_empty_workdir_still_writes_manifest(tmp_path: Path) -> None:
    out = tmp_path / "EMPTY.tar.gz"
    bundle = TicketBundle(
        workdir=tmp_path,
        tracker="github",
        ticket_id="ZZ-1",
        selector=_selector_without_git(tmp_path),
    )
    manifest = bundle.assemble(out=out)
    assert out.exists()
    # Always at least git/commits.json (an empty list payload).
    assert any(e.arcname == "git/commits.json" for e in manifest.files)


# ---------------------------------------------------------------------------
# Sign + verify
# ---------------------------------------------------------------------------


def test_sign_and_verify_roundtrip(workdir: Path) -> None:
    out = workdir / "ENG-42.tar.gz"
    bundle = TicketBundle(
        workdir=workdir,
        tracker="github",
        ticket_id="ENG-42",
        selector=_selector_without_git(workdir),
    )
    bundle.assemble(out=out)

    priv_pem, pub_pem = generate_keypair()
    jws_path = bundle.sign(private_key_pem=priv_pem, kid="lineage-2026")

    card = AgentCard(agent_id="agent:test", kid="lineage-2026", public_key_pem=pub_pem)
    assert TicketBundle.verify(out, jws_path, card) is True


def test_sign_without_assemble_raises(workdir: Path) -> None:
    bundle = TicketBundle(workdir=workdir, tracker="github", ticket_id="ENG-42")
    priv_pem, _pub_pem = generate_keypair()
    with pytest.raises(RuntimeError):
        bundle.sign(private_key_pem=priv_pem, kid="k1")


def test_verify_fails_with_wrong_card(workdir: Path) -> None:
    out = workdir / "ENG-42.tar.gz"
    bundle = TicketBundle(
        workdir=workdir,
        tracker="github",
        ticket_id="ENG-42",
        selector=_selector_without_git(workdir),
    )
    bundle.assemble(out=out)
    priv_pem, _pub_pem = generate_keypair()
    jws_path = bundle.sign(private_key_pem=priv_pem, kid="k1")
    _other_priv, other_pub = generate_keypair()
    wrong_card = AgentCard(agent_id="agent:test", kid="k1", public_key_pem=other_pub)
    assert TicketBundle.verify(out, jws_path, wrong_card) is False


# ---------------------------------------------------------------------------
# Tampering detection
# ---------------------------------------------------------------------------


def _rewrite_archive_member(archive: Path, arcname: str, new_data: bytes) -> None:
    """Rewrite *archive* so that *arcname* contains *new_data* (others unchanged)."""
    with tarfile.open(archive, mode="r:*") as src:
        members = src.getmembers()
        contents: dict[str, bytes] = {}
        for member in members:
            fp = src.extractfile(member)
            contents[member.name] = fp.read() if fp is not None else b""
    contents[arcname] = new_data
    with tarfile.open(archive, mode="w:gz") as dst:
        for name, data in contents.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o644
            dst.addfile(info, io.BytesIO(data))


def test_tampering_with_bundled_file_is_detected(workdir: Path) -> None:
    out = workdir / "ENG-42.tar.gz"
    bundle = TicketBundle(
        workdir=workdir,
        tracker="github",
        ticket_id="ENG-42",
        selector=_selector_without_git(workdir),
    )
    bundle.assemble(out=out)
    priv_pem, pub_pem = generate_keypair()
    jws_path = bundle.sign(private_key_pem=priv_pem, kid="k1")
    card = AgentCard(agent_id="agent:test", kid="k1", public_key_pem=pub_pem)
    # Sanity: untampered archive verifies.
    assert TicketBundle.verify(out, jws_path, card) is True

    # Replace a bundled transcript with different bytes.
    _rewrite_archive_member(
        out,
        "transcripts/agent-backend-ENG-42.jsonl",
        b'{"agent": "backend", "turn": 1, "text": "TAMPERED"}\n',
    )
    assert TicketBundle.verify(out, jws_path, card) is False


def test_tampering_with_manifest_is_detected(workdir: Path) -> None:
    out = workdir / "ENG-42.tar.gz"
    bundle = TicketBundle(
        workdir=workdir,
        tracker="github",
        ticket_id="ENG-42",
        selector=_selector_without_git(workdir),
    )
    bundle.assemble(out=out)
    priv_pem, pub_pem = generate_keypair()
    jws_path = bundle.sign(private_key_pem=priv_pem, kid="k1")
    card = AgentCard(agent_id="agent:test", kid="k1", public_key_pem=pub_pem)

    # Read current manifest, flip a field, write it back. This breaks the
    # signature because the canonical bytes change.
    with tarfile.open(out, mode="r:*") as tf:
        fp = tf.extractfile(tf.getmember("manifest.json"))
        assert fp is not None
        manifest_payload = json.loads(fp.read())
    manifest_payload["ticket_id"] = "TAMPERED"
    tampered_bytes = (json.dumps(manifest_payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _rewrite_archive_member(out, "manifest.json", tampered_bytes)

    assert TicketBundle.verify(out, jws_path, card) is False


def test_missing_jws_returns_false(workdir: Path) -> None:
    out = workdir / "ENG-42.tar.gz"
    bundle = TicketBundle(
        workdir=workdir,
        tracker="github",
        ticket_id="ENG-42",
        selector=_selector_without_git(workdir),
    )
    bundle.assemble(out=out)
    _priv, pub = generate_keypair()
    card = AgentCard(agent_id="agent:test", kid="k1", public_key_pem=pub)
    missing = workdir / "does-not-exist.jws"
    assert TicketBundle.verify(out, missing, card) is False


# ---------------------------------------------------------------------------
# Manifest dataclass smoke
# ---------------------------------------------------------------------------


def test_jsonl_probe_requires_same_record_for_tracker_and_ticket(tmp_path: Path) -> None:
    """Cross-record matches must NOT satisfy the JSONL probe.

    A file where one record carries the tracker and a *different* record
    carries the ticket id should not be picked up.
    """
    sdd = tmp_path / ".sdd" / "lineage"
    sdd.mkdir(parents=True)
    # Cross-record: tracker on line 1, ticket_id on line 2 -- must NOT match.
    (sdd / "cross.jsonl").write_text(
        '{"tracker": "github", "ticket_id": "OTHER-1"}\n{"tracker": "jira", "ticket_id": "ENG-42"}\n',
        encoding="utf-8",
    )
    # Same record: tracker + ticket_id together -- must match.
    (sdd / "same.jsonl").write_text(
        '{"tracker": "github", "ticket_id": "ENG-42"}\n',
        encoding="utf-8",
    )
    out = tmp_path / "ENG-42.tar.gz"
    bundle = TicketBundle(
        workdir=tmp_path,
        tracker="github",
        ticket_id="ENG-42",
        selector=_selector_without_git(tmp_path),
    )
    manifest = bundle.assemble(out=out)
    arcnames = {entry.arcname for entry in manifest.files}
    assert "lineage/same.jsonl" in arcnames
    assert "lineage/cross.jsonl" not in arcnames


def test_verify_rejects_extra_archive_members(workdir: Path) -> None:
    """Smuggled members not listed in the manifest must fail verify."""
    out = workdir / "ENG-42.tar.gz"
    bundle = TicketBundle(
        workdir=workdir,
        tracker="github",
        ticket_id="ENG-42",
        selector=_selector_without_git(workdir),
    )
    bundle.assemble(out=out)
    priv_pem, pub_pem = generate_keypair()
    jws_path = bundle.sign(private_key_pem=priv_pem, kid="k1")
    card = AgentCard(agent_id="agent:test", kid="k1", public_key_pem=pub_pem)
    assert TicketBundle.verify(out, jws_path, card) is True

    # Append an extra file that the manifest does not list.
    with tarfile.open(out, mode="r:*") as src:
        contents: dict[str, bytes] = {}
        for member in src.getmembers():
            fp = src.extractfile(member)
            contents[member.name] = fp.read() if fp is not None else b""
    contents["smuggled/payload.bin"] = b"extra"
    with tarfile.open(out, mode="w:gz") as dst:
        for name, data in contents.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o644
            dst.addfile(info, io.BytesIO(data))

    assert TicketBundle.verify(out, jws_path, card) is False


def test_verify_rejects_unsupported_schema_version(workdir: Path) -> None:
    """A manifest with a schema_version outside the supported set fails."""
    out = workdir / "ENG-42.tar.gz"
    bundle = TicketBundle(
        workdir=workdir,
        tracker="github",
        ticket_id="ENG-42",
        selector=_selector_without_git(workdir),
    )
    bundle.assemble(out=out)
    priv_pem, pub_pem = generate_keypair()
    jws_path = bundle.sign(private_key_pem=priv_pem, kid="k1")
    card = AgentCard(agent_id="agent:test", kid="k1", public_key_pem=pub_pem)

    # Rewrite the manifest with a bumped schema_version that this
    # release does not understand. This also invalidates the signature,
    # but the schema-version rejection should fire BEFORE the sig check.
    with tarfile.open(out, mode="r:*") as tf:
        fp = tf.extractfile(tf.getmember("manifest.json"))
        assert fp is not None
        payload = json.loads(fp.read())
    payload["schema_version"] = max(SUPPORTED_SCHEMA_VERSIONS) + 99
    new_bytes = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _rewrite_archive_member(out, "manifest.json", new_bytes)

    assert TicketBundle.verify(out, jws_path, card) is False


def test_manifest_canonical_bytes_are_deterministic() -> None:
    m = BundleManifest(
        schema_version=1,
        created_at="2026-05-19T00:00:00+00:00",
        bernstein_version="0.0.0",
        tracker="github",
        ticket_id="ENG-42",
        files=[
            ManifestEntry(arcname="b.txt", size_bytes=1, sha256="b" * 64, section="x"),
            ManifestEntry(arcname="a.txt", size_bytes=1, sha256="a" * 64, section="x"),
        ],
    )
    encoded_once = m.canonical_bytes()
    encoded_twice = m.canonical_bytes()
    assert encoded_once == encoded_twice
    # Keys are sorted -> "bernstein_version" precedes "commits".
    text = encoded_once.decode("utf-8")
    assert text.index('"bernstein_version"') < text.index('"commits"')
