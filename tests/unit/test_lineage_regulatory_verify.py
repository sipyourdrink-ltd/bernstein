"""Regulatory-lineage verification tests (schema v2 + Ed25519 + exporter).

Covers compliance edge-cases not exercised by ``test_lineage_record.py``,
``test_lineage_signer.py``, or ``test_lineage_export.py``:

* schema-v2 forward-compat: a record carrying unknown future fields still
  reads back without crashing;
* signed-tamper detection: mutating ``regulatory_class`` on a signed
  record breaks the customer signature even when the WAL hash chain
  itself is rebuilt;
* signature edge-cases: empty-string and malformed-base64 signatures
  are not silently accepted; the empty-string sentinel is treated as
  *unsigned* rather than as a 0-byte signature so an unrelated
  formatter typo cannot produce a misleading tamper alert;
* exporter escaping: ``regulatory_class`` containing CSV-special bytes
  (commas, quotes, newlines) round-trips through Python's ``csv``
  reader; HTML output escapes XSS payloads in *every* operator-supplied
  field and never inlines an unescaped ``<script>``; JSON-LD output is
  always valid JSON regardless of unicode escapes;
* path-traversal hardening: ``LineageWriter.for_run`` rejects ``run_id``
  values that contain a path separator (which would silently land
  records in a sibling directory the reader does not glob);
* ``run_id`` traversal in the CLI exporter (``../etc/passwd``) is
  rejected before any file write;
* large-run exporter: 5k records stream through ``iter_records`` and
  the renderer emits one row per record without exceeding a generous
  memory budget.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import re

import pytest

resource = pytest.importorskip("resource")
import sys
from html.parser import HTMLParser
from pathlib import Path

from click.testing import CliRunner
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.cli.commands.lineage_export_cmd import (
    _record_row,
    lineage_export_cmd,
    render_csv,
    render_html,
    render_jsonld,
)
from bernstein.core.persistence.lineage import (
    AgentRef,
    ArtifactRef,
    LineageReader,
    LineageRecord,
    LineageRunIdError,
    LineageWriter,
    canonical_record_bytes,
    decode_signature,
    record_to_dict,
    verify_run_chain,
)
from bernstein.core.persistence.lineage_signer import (
    Ed25519FileKeySigner,
    Ed25519PublicKeyVerifier,
)
from bernstein.core.persistence.wal import WALWriter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ephemeral_pem(tmp_path: Path, name: str = "key.pem") -> Path:
    """Drop a fresh PKCS#8 PEM Ed25519 private key in *tmp_path*."""
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    out = tmp_path / name
    out.write_bytes(pem)
    return out


def _row_for(record: LineageRecord) -> dict[str, object]:
    return _record_row(record)


def _rehash_entry(entry: dict[str, object]) -> dict[str, object]:
    """Recompute ``entry_hash`` after we mutate a WAL line in place.

    Used by the tamper-detection tests to prove the *signature* (not
    the WAL chain) caught the mutation.
    """
    entry.pop("entry_hash", None)
    canonical = json.dumps(entry, sort_keys=True, separators=(",", ":"))
    entry["entry_hash"] = hashlib.sha256(canonical.encode()).hexdigest()
    return entry


def _make_record(
    path: str = "src/foo.py",
    *,
    regulatory_class: str | None = None,
) -> LineageRecord:
    return LineageRecord(
        output_artifact=ArtifactRef(path=path, sha256="a" * 64, line_start=1, line_end=10),
        inputs=[ArtifactRef(path="in.py", sha256="b" * 64)],
        producer=AgentRef(agent_id="agent", run_id="run-test", tick_id="t-0"),
        prompt_sha="c" * 64,
        model="claude-sonnet",
        cost_usd=0.01,
        tokens=100,
        timestamp=1700000000.0,
        regulatory_class=regulatory_class,
    )


# ---------------------------------------------------------------------------
# Schema forward-compat
# ---------------------------------------------------------------------------


class TestSchemaForwardCompat:
    def test_unknown_future_fields_read_through(self, tmp_path: Path) -> None:
        """A record stamped with ``schema_version=99`` and unknown extras
        still surfaces the v1/v2 fields the reader knows about.

        Compliance teams replay archived runs that may have been
        written by a newer Bernstein release; a hard read failure
        would lose the chain.
        """
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        wal = WALWriter(run_id="run-future", sdd_dir=sdd)
        wal.append(
            decision_type="lineage",
            inputs={
                "inputs": [],
                "producer": {"agent_id": "a", "run_id": "run-future", "tick_id": None},
                "prompt_sha": "p",
                "model": "m",
                # v3 hypothetical extra field - must not crash the v2 reader
                "future_classification": "TLP-RED",
            },
            output={
                "output_artifact": {
                    "path": "src/x.py",
                    "sha256": "a" * 64,
                    "byte_start": None,
                    "byte_end": None,
                    "line_start": None,
                    "line_end": None,
                },
                "cost_usd": 0.01,
                "tokens": 100,
                "timestamp": 1.0,
                "schema_version": 99,
                "regulatory_class": "future_only_class",
                "customer_signature": None,
                # additional v3 hypothetical extra field
                "merkle_root": "deadbeef" * 8,
            },
            actor="a",
        )
        rec = LineageReader(sdd).lookup("src/x.py", run_id="run-future")[0]
        assert rec.schema_version == 99  # preserved as-is
        assert rec.regulatory_class == "future_only_class"
        assert rec.customer_signature is None

    def test_v1_record_with_no_schema_version_reads_as_v1(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        wal = WALWriter(run_id="run-legacy", sdd_dir=sdd)
        wal.append(
            decision_type="lineage",
            inputs={
                "inputs": [],
                "producer": {"agent_id": "a", "run_id": "run-legacy"},
                "prompt_sha": "x",
                "model": "y",
            },
            output={
                "output_artifact": {
                    "path": "src/x.py",
                    "sha256": "a" * 64,
                    "byte_start": None,
                    "byte_end": None,
                    "line_start": None,
                    "line_end": None,
                },
                "cost_usd": 0.0,
                "tokens": 0,
                "timestamp": 1.0,
            },
            actor="a",
        )
        rec = LineageReader(sdd).lookup("src/x.py", run_id="run-legacy")[0]
        assert rec.schema_version == 1
        assert rec.regulatory_class is None
        assert rec.customer_signature is None


# ---------------------------------------------------------------------------
# Signature edge-cases
# ---------------------------------------------------------------------------


class TestSignatureEdgeCases:
    def test_tampered_regulatory_class_breaks_signature(self, tmp_path: Path) -> None:
        """Mutating ``regulatory_class`` after signing must invalidate the
        customer signature, even when the WAL hash chain is recomputed
        so the orchestrator's chain check still passes.

        This is the load-bearing assertion for "an operator cannot
        rewrite the compliance class without the customer auditor
        noticing".
        """
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        signer = Ed25519FileKeySigner.from_path(_ephemeral_pem(tmp_path))
        verifier = Ed25519PublicKeyVerifier.from_raw(signer.public_key_bytes())
        writer = LineageWriter.for_run("run-tamper", sdd, signer=signer)
        writer.emit(_make_record(regulatory_class="production_detection_rule"))

        # Tamper: rewrite the class on disk; recompute the WAL entry hash so
        # the chain itself stays intact (we want the *signature* to catch this,
        # not the WAL chain).
        wal_path = sdd / "runtime" / "wal" / "run-tamper.wal.jsonl"
        lines = wal_path.read_text().splitlines()
        entry = json.loads(lines[0])
        entry["output"]["regulatory_class"] = "low_risk_test_only"
        _rehash_entry(entry)
        lines[0] = json.dumps(entry, separators=(",", ":"))
        wal_path.write_text("\n".join(lines) + "\n")

        result = verify_run_chain(sdd, "run-tamper", verifier=verifier)
        # WAL chain rebuilt cleanly, so signature is the only barrier left.
        assert result.ok is False
        assert any("signature failed to verify" in e for e in result.errors)

    def test_empty_string_signature_treated_as_unsigned(self, tmp_path: Path) -> None:
        """``customer_signature=""`` is a misconfiguration sentinel, not a
        zero-byte signature.

        Without normalisation, ``base64.b64decode("")`` returns ``b""``
        and any Ed25519 verifier rejects it -- producing a misleading
        ``signature failed to verify`` for what is in fact an unsigned
        record. The reader collapses empty strings to ``None`` so the
        verifier path simply skips it.
        """
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        wal = WALWriter(run_id="run-empty-sig", sdd_dir=sdd)
        wal.append(
            decision_type="lineage",
            inputs={
                "inputs": [],
                "producer": {"agent_id": "a", "run_id": "run-empty-sig"},
                "prompt_sha": "p",
                "model": "m",
            },
            output={
                "output_artifact": {
                    "path": "x.py",
                    "sha256": "a" * 64,
                    "byte_start": None,
                    "byte_end": None,
                    "line_start": None,
                    "line_end": None,
                },
                "cost_usd": 0.0,
                "tokens": 0,
                "timestamp": 1.0,
                "schema_version": 2,
                "regulatory_class": "",  # empty -> normalise to None
                "customer_signature": "",  # empty -> normalise to None
            },
            actor="a",
        )
        rec = LineageReader(sdd).lookup("x.py", run_id="run-empty-sig")[0]
        assert rec.customer_signature is None
        assert rec.regulatory_class is None

        # Now a verifier sees no signature → no error.
        signer = Ed25519FileKeySigner.from_path(_ephemeral_pem(tmp_path))
        verifier = Ed25519PublicKeyVerifier.from_raw(signer.public_key_bytes())
        result = verify_run_chain(sdd, "run-empty-sig", verifier=verifier)
        assert result.ok is True
        assert result.errors == []
        assert result.record_count == 1

    def test_malformed_base64_signature_reports_clean_error(self, tmp_path: Path) -> None:
        """A signature that isn't legal base64 must produce a *single*,
        clearly-labelled error rather than crashing the whole verify
        pass."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        signer = Ed25519FileKeySigner.from_path(_ephemeral_pem(tmp_path))
        writer = LineageWriter.for_run("run-malformed", sdd, signer=signer)
        writer.emit(_make_record(regulatory_class="foo"))

        wal_path = sdd / "runtime" / "wal" / "run-malformed.wal.jsonl"
        lines = wal_path.read_text().splitlines()
        entry = json.loads(lines[0])
        entry["output"]["customer_signature"] = "!!not-base64!!"
        _rehash_entry(entry)
        lines[0] = json.dumps(entry, separators=(",", ":"))
        wal_path.write_text("\n".join(lines) + "\n")

        verifier = Ed25519PublicKeyVerifier.from_raw(signer.public_key_bytes())
        result = verify_run_chain(sdd, "run-malformed", verifier=verifier)
        assert result.ok is False
        # Exactly one signature error; no unhandled traceback.
        sig_errors = [e for e in result.errors if "malformed signature" in e]
        assert len(sig_errors) == 1

    def test_forged_signature_against_v2_payload_fails(self, tmp_path: Path) -> None:
        """An attacker who forges a valid 64-byte Ed25519-shaped blob (random
        bytes, valid base64) but does not have the customer's private key
        cannot get past :func:`verify_run_chain`."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        signer = Ed25519FileKeySigner.from_path(_ephemeral_pem(tmp_path))
        writer = LineageWriter.for_run("run-forge", sdd, signer=signer)
        writer.emit(_make_record(regulatory_class="policy_edit"))

        # Replace with a syntactically valid but unauthored signature
        wal_path = sdd / "runtime" / "wal" / "run-forge.wal.jsonl"
        lines = wal_path.read_text().splitlines()
        entry = json.loads(lines[0])
        forged = base64.b64encode(b"\x00" * 64).decode("ascii")
        entry["output"]["customer_signature"] = forged
        _rehash_entry(entry)
        lines[0] = json.dumps(entry, separators=(",", ":"))
        wal_path.write_text("\n".join(lines) + "\n")

        verifier = Ed25519PublicKeyVerifier.from_raw(signer.public_key_bytes())
        result = verify_run_chain(sdd, "run-forge", verifier=verifier)
        assert result.ok is False
        assert any("signature failed to verify" in e for e in result.errors)

    def test_re_emit_keeps_both_signatures(self, tmp_path: Path) -> None:
        """The lineage trail is append-only: re-emitting the same record
        leaves *both* WAL entries intact, each carrying its own
        signature.

        Compliance auditors expect history (last-wins is incorrect).
        """
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        signer = Ed25519FileKeySigner.from_path(_ephemeral_pem(tmp_path))
        writer = LineageWriter.for_run("run-double", sdd, signer=signer)
        writer.emit(_make_record(regulatory_class="v1"))
        writer.emit(_make_record(regulatory_class="v2"))

        records = LineageReader(sdd).lookup("src/foo.py", run_id="run-double")
        assert len(records) == 2
        assert {r.regulatory_class for r in records} == {"v1", "v2"}
        assert all(r.customer_signature is not None for r in records)
        # Each signature is over its own canonical bytes
        verifier = Ed25519PublicKeyVerifier.from_raw(signer.public_key_bytes())
        for rec in records:
            assert rec.customer_signature is not None
            sig = decode_signature(rec.customer_signature)
            assert verifier.verify(canonical_record_bytes(rec), sig)

    def test_two_keys_one_run_first_key_rejects_second(self, tmp_path: Path) -> None:
        """Two writers, one run, two different customer keys → an auditor
        with key-A's pubkey accepts the first record and rejects the
        second.

        This is exactly the diagnostic an auditor needs when the
        customer rotates the signing key mid-run -- the verify pass
        must surface both records (not abort early) and label which one
        failed.
        """
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        signer_a = Ed25519FileKeySigner.from_path(_ephemeral_pem(tmp_path, "a.pem"))
        signer_b = Ed25519FileKeySigner.from_path(_ephemeral_pem(tmp_path, "b.pem"))

        writer_a = LineageWriter.for_run("run-rotate", sdd, signer=signer_a)
        writer_a.emit(_make_record(path="src/a.py", regulatory_class="x"))
        writer_b = LineageWriter.for_run("run-rotate", sdd, signer=signer_b)
        writer_b.emit(_make_record(path="src/b.py", regulatory_class="x"))

        verifier_a = Ed25519PublicKeyVerifier.from_raw(signer_a.public_key_bytes())
        result = verify_run_chain(sdd, "run-rotate", verifier=verifier_a)
        assert result.ok is False
        # Exactly one record fails; the bad one is the b.py record.
        bad = [e for e in result.errors if "signature failed to verify" in e]
        assert len(bad) == 1
        assert "src/b.py" in bad[0]


# ---------------------------------------------------------------------------
# Run-id path traversal
# ---------------------------------------------------------------------------


class TestRunIdValidation:
    @pytest.mark.parametrize(
        "bad_run_id",
        [
            "foo/bar",
            "foo\\bar",
            "../escape",
            "..",
            ".",
            "",
            "with\x00nul",
        ],
    )
    def test_for_run_rejects_path_separators_and_traversal(self, tmp_path: Path, bad_run_id: str) -> None:
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        with pytest.raises(LineageRunIdError):
            LineageWriter.for_run(bad_run_id, sdd)

    def test_safe_run_ids_pass(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        # Reasonable run-id shapes the orchestrator actually uses
        for ok in ("run-1", "r-2026-05-05", "abc123", "with.dots.but.no.dotdot"):
            LineageWriter.for_run(ok, sdd).emit(_make_record(path=f"x-{ok}.py"))

    def test_exporter_with_traversal_run_id_returns_no_records(self, tmp_path: Path) -> None:
        """A traversal-shaped *query* run_id must not blow up the exporter --
        it just yields zero records (because the WAL glob is non-recursive
        and the file does not exist)."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        LineageWriter.for_run("run-1", sdd).emit(_make_record())
        out = tmp_path / "audit.csv"
        runner = CliRunner()
        result = runner.invoke(
            lineage_export_cmd,
            [
                "../../etc/passwd",
                "--format",
                "csv",
                "--output",
                str(out),
                "--workdir",
                str(tmp_path),
            ],
        )
        # Exit-2 is the documented "no records" path; importantly, no
        # write actually happens.
        assert result.exit_code != 0
        assert not out.exists()


# ---------------------------------------------------------------------------
# CSV escaping
# ---------------------------------------------------------------------------


class TestCsvEscaping:
    def test_regulatory_class_with_csv_metacharacters_round_trips(self) -> None:
        """The exporter must emit RFC-4180-correct quoting for fields
        containing commas, quotes, and embedded newlines.

        This is the "operator types a sentence into the regulatory_class
        field" failure mode -- if the CSV writer emits raw bytes the
        downstream GRC importer fragments the row and the auditor sees
        garbage.
        """
        nasty = 'gdpr, "right-of-erasure"\nrow2,injected'
        rec = _make_record(regulatory_class=nasty)
        text = render_csv([_row_for(rec)])
        # Round-trip via Python's CSV reader (the same parser most
        # GRC importers ship)
        rows = list(csv.DictReader(io.StringIO(text)))
        assert len(rows) == 1
        assert rows[0]["regulatory_class"] == nasty
        # Sanity: the row count check above already proves the embedded
        # newline did not fragment the row. Cross-check that the writer
        # emitted CRLF record terminators (the RFC-4180 default that
        # Python's ``csv`` reader expects); without these the embedded
        # ``\n`` inside the quoted field could be misread by tools that
        # split on CRLF only.
        assert text.count("\r\n") == 2  # header + 1 data row

    def test_regulatory_class_with_unicode_survives_round_trip(self) -> None:
        """Non-ASCII (e.g. cyrillic) characters round-trip without
        mojibake."""
        rec = _make_record(regulatory_class="Конфиденциально - TLP:AMBER")
        text = render_csv([_row_for(rec)])
        rows = list(csv.DictReader(io.StringIO(text)))
        assert rows[0]["regulatory_class"] == "Конфиденциально - TLP:AMBER"

    def test_csv_preserves_full_signature_unlike_html(self) -> None:
        """The HTML view truncates the signature for readability; the CSV
        export must keep the full base64 string so a downstream
        verifier can re-check it."""
        full_sig = base64.b64encode(b"x" * 64).decode("ascii")
        rec = LineageRecord(
            output_artifact=ArtifactRef(path="x.py", sha256="a" * 64),
            producer=AgentRef(agent_id="a", run_id="r"),
            customer_signature=full_sig,
        )
        text = render_csv([_row_for(rec)])
        rows = list(csv.DictReader(io.StringIO(text)))
        assert rows[0]["customer_signature"] == full_sig


# ---------------------------------------------------------------------------
# HTML escaping (XSS hardening)
# ---------------------------------------------------------------------------


class _ScriptDetector(HTMLParser):
    """Minimal parser that flags any ``<script>`` tag in the output."""

    def __init__(self) -> None:
        super().__init__()
        self.script_tags: list[tuple[str, list[tuple[str, str | None]]]] = []
        self.event_handlers: list[tuple[str, str]] = []
        self.has_javascript_url = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            self.script_tags.append((tag, attrs))
        for name, value in attrs:
            if value is None:
                continue
            if name.startswith("on"):
                self.event_handlers.append((name, value))
            if name in ("href", "src") and value.strip().lower().startswith("javascript:"):
                self.has_javascript_url = True


class TestHtmlXssHardening:
    """The HTML exporter ships verbatim into a customer's compliance
    package. *Any* unescaped operator-supplied byte is a hole."""

    @pytest.mark.parametrize(
        "payload",
        [
            "<script>alert(1)</script>",
            '"><script>alert(1)</script>',
            "</td></tr><script>alert(1)</script><!--",
            "<img src=x onerror=alert(1)>",
            "<iframe src=javascript:alert(1)></iframe>",
        ],
    )
    def test_xss_in_regulatory_class(self, payload: str) -> None:
        rec = _make_record(regulatory_class=payload)
        text = render_html([_row_for(rec)], run_id="r-1")
        det = _ScriptDetector()
        det.feed(text)
        # The exporter inlines exactly one trusted <style> tag; we only
        # want to assert there are *zero* untrusted <script> tags
        # (which we never emit deliberately).
        assert det.script_tags == []
        assert det.event_handlers == []
        assert det.has_javascript_url is False
        # Pedantic: the literal "<script" must not occur anywhere
        # outside an HTML comment in the document.
        assert "<script" not in text.lower()

    def test_xss_in_run_id(self) -> None:
        rec = _make_record(regulatory_class="x")
        text = render_html(
            [_row_for(rec)],
            run_id='"><script>alert(1)</script>',
        )
        det = _ScriptDetector()
        det.feed(text)
        assert det.script_tags == []
        # The escaped payload must still be discoverable in the markup
        # (so a reviewer can see what the operator sent).
        assert "&lt;script&gt;" in text

    def test_xss_in_agent_id(self) -> None:
        rec = LineageRecord(
            output_artifact=ArtifactRef(path="x.py", sha256="a" * 64),
            producer=AgentRef(
                agent_id='"><svg/onload=alert(1)>',
                run_id="r",
            ),
        )
        text = render_html([_row_for(rec)], run_id="r")
        det = _ScriptDetector()
        det.feed(text)
        # No *live* event handler attribute (escaped ``onload=`` text inside
        # an escaped ``&lt;svg ...&gt;`` is inert; we rely on the parser
        # to confirm there is no real ``<svg>`` element with ``onload``).
        assert det.event_handlers == []
        # The escaped payload is visible in the markup (so a reviewer can
        # see what the operator submitted).
        assert "&lt;svg/onload=alert(1)&gt;" in text

    def test_xss_in_output_path_and_inputs(self) -> None:
        rec = LineageRecord(
            output_artifact=ArtifactRef(
                path="<script>alert('out')</script>",
                sha256="a" * 64,
            ),
            inputs=[
                ArtifactRef(
                    path="<script>alert('in')</script>",
                    sha256="b" * 64,
                ),
            ],
            producer=AgentRef(agent_id="a", run_id="r"),
        )
        text = render_html([_row_for(rec)], run_id="r")
        det = _ScriptDetector()
        det.feed(text)
        assert det.script_tags == []
        assert "<script>alert" not in text


# ---------------------------------------------------------------------------
# JSON-LD validity
# ---------------------------------------------------------------------------


class TestJsonLdValidity:
    def test_unicode_and_html_chars_produce_valid_json(self) -> None:
        """Whatever the operator types, the JSON-LD output must remain
        parseable JSON.

        The schema.org ``@context`` URL is fixed, so a downstream
        JSON-LD library can be relied on to dereference the document.
        """
        rec = _make_record(
            regulatory_class='</script><script>alert("</context>")</script>',
        )
        text = render_jsonld([_row_for(rec)], run_id="r-1")
        doc = json.loads(text)
        assert doc["@context"] == "https://schema.org"
        assert doc["@type"] == "ItemList"
        # Round-tripped value matches input
        props = {p["name"]: p["value"] for p in doc["itemListElement"][0]["additionalProperty"]}
        assert props["regulatory_class"] == rec.regulatory_class

    def test_jsonld_preserves_action_shape_for_each_record(self) -> None:
        rec = _make_record(regulatory_class="x")
        text = render_jsonld([_row_for(rec)], run_id="r-1")
        doc = json.loads(text)
        action = doc["itemListElement"][0]
        assert action["@type"] == "Action"
        assert action["actionStatus"] == "CompletedActionStatus"
        # ``object`` and ``instrument`` are the schema.org keys auditor
        # tooling graph-walks on.
        assert action["object"]["identifier"] == "src/foo.py"
        assert action["instrument"][0]["identifier"] == "in.py"
        # ``agent`` carries the producer.
        assert action["agent"]["identifier"] == "agent"


# ---------------------------------------------------------------------------
# Bundle / record_to_dict v2 fidelity
# ---------------------------------------------------------------------------


class TestRecordToDict:
    def test_record_to_dict_includes_v2_fields(self) -> None:
        rec = _make_record(regulatory_class="rc")
        rec_with_sig = LineageRecord(
            output_artifact=rec.output_artifact,
            inputs=rec.inputs,
            producer=rec.producer,
            prompt_sha=rec.prompt_sha,
            model=rec.model,
            cost_usd=rec.cost_usd,
            tokens=rec.tokens,
            timestamp=rec.timestamp,
            regulatory_class="rc",
            customer_signature="sig-base64==",
            schema_version=2,
        )
        d = record_to_dict(rec_with_sig)
        assert d["regulatory_class"] == "rc"
        assert d["customer_signature"] == "sig-base64=="
        assert d["schema_version"] == 2


# ---------------------------------------------------------------------------
# Streaming / large-run exporter
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="resource.getrusage is POSIX-only",
)
class TestExporterScale:
    """Compliance customers can have multi-thousand-record runs.

    The exporter currently materialises the whole list (``list(...)``)
    -- the goal of this test is not to *prove* O(1) memory but to
    catch any catastrophic blow-up (e.g. accidentally O(N²) string
    concatenation in the renderer)."""

    def test_5k_records_export_under_memory_budget(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        writer = LineageWriter.for_run("run-big", sdd)
        for i in range(5000):
            writer.emit(
                LineageRecord(
                    output_artifact=ArtifactRef(
                        path=f"src/f{i % 50}.py",
                        sha256="a" * 64,
                        line_start=1,
                        line_end=2,
                    ),
                    inputs=[],
                    producer=AgentRef(agent_id="a", run_id="run-big"),
                    prompt_sha="p" * 64,
                    model="m",
                    timestamp=float(i),
                    regulatory_class="cat",
                )
            )

        rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        out = tmp_path / "audit.csv"
        runner = CliRunner()
        result = runner.invoke(
            lineage_export_cmd,
            [
                "run-big",
                "--format",
                "csv",
                "--output",
                str(out),
                "--workdir",
                str(tmp_path),
            ],
        )
        rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        assert result.exit_code == 0, result.output

        # ru_maxrss is bytes on macOS, kilobytes on Linux. Either way
        # the *delta* should be far under 250 MB for 5k tiny records;
        # we use a generous budget to keep this stable across CI flavours.
        delta = max(rss_after - rss_before, 0)
        delta_bytes_real = delta if sys.platform == "darwin" else delta * 1024
        assert delta_bytes_real < 250 * 1024 * 1024, (
            f"exporter consumed {delta_bytes_real / 1024 / 1024:.1f} MiB on 5k records"
        )

        # And the file actually contains 5000 + 1 (header) lines. The CSV
        # writer emits CRLF line endings, but ``read_text`` performs
        # universal-newline translation, so we count ``\n``.
        text = out.read_text()
        assert text.count("\n") == 5001

    def test_html_5k_records_renders_without_crash(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        writer = LineageWriter.for_run("run-htmlbig", sdd)
        for i in range(5000):
            writer.emit(
                LineageRecord(
                    output_artifact=ArtifactRef(path=f"src/f{i}.py", sha256="a" * 64, line_start=1, line_end=2),
                    inputs=[],
                    producer=AgentRef(agent_id="a", run_id="run-htmlbig"),
                    prompt_sha="p",
                    model="m",
                    timestamp=float(i),
                )
            )
        out = tmp_path / "audit.html"
        runner = CliRunner()
        result = runner.invoke(
            lineage_export_cmd,
            [
                "run-htmlbig",
                "--format",
                "html",
                "--output",
                str(out),
                "--workdir",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        text = out.read_text()
        # 5000 <tr> rows in tbody + 1 header row
        rows = re.findall(r"<tr>", text)
        assert len(rows) == 5001
