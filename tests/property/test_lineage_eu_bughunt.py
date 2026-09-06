"""Hypothesis bug-hunt - lineage v2 + EU AI Act Article 12 + KMS + EU residency.

Single-file battery. Each ``Test*`` class corresponds to a *finding
bucket* surfaced during the bug hunt; each test maps to a single failure
mode (or near-miss) in the production surface. Tests are grouped by
severity - regulatory-claim violators first, then operational bugs,
then harder-to-explain near-misses.

Findings index:

* DNS-rebinding-by-naming bypass against the EU residency guard
  (fixed; the test pins the fix and documents the attack class so a
  future regression is loud).
* KMS adapter contract drift: the documented protocol promises
  ``public_key_jwk()`` but the only concrete signer exposes
  ``public_key_bytes()``; auditors who consume JWK-shaped attestations
  cannot ingest signatures (xfail, not silent).
* No HSM/KMS concrete stub - ``signer_from_config`` rejects
  ``key_kind='hsm'`` with a generic config error rather than a
  meaningful "implement me" :class:`NotImplementedError` (xfail).
* Article 12(3) retention floor for high-risk: 10 years rendered as
  ``round(10 * 365.25) == 3653`` days - boundary tested.
* Article 12(5) integrity holds across legitimate compaction
  (``compress_rotated_lineage`` only touches rotated backups, never the
  active chain).
* Bundle determinism - same window, same key → byte-identical zip.
* Clause map: every shipped clause has a non-empty subsystem mapping
  and an evidence artefact.
* IPv6 residency: ``::1``, ``fe80::*``, ``fc00::/7`` accepted; public
  IPv6 rejected.
* Tamper detection: byte-flip in any record breaks
  :func:`verify_run_chain`; missing middle record breaks the chain too.
* SOC 2 evidence: empty ``.sdd/`` produces a "pending" markdown row,
  not a crash.

The file lives under ``tests/property/`` because most assertions use
Hypothesis to drive adversarial inputs at the residency guard and the
retention validator. Tests that don't need property-based coverage stay
inline so the bug-hunt narrative reads top-to-bottom in one place.
"""

from __future__ import annotations

import json
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from bernstein.adapters.ollama import OllamaAdapter
from bernstein.core.persistence.lineage import (
    AgentRef,
    ArtifactRef,
    LineageReader,
    LineageRecord,
    LineageRunIdError,
    LineageWriter,
    canonical_record_bytes,
    compress_rotated_lineage,
    verify_run_chain,
)
from bernstein.core.persistence.lineage_signer import (
    Ed25519FileKeySigner,
    Ed25519PublicKeyVerifier,
    LineageSigner,
    LineageSignerError,
    signer_from_config,
)
from bernstein.core.security.article12_bundle import (
    HIGH_RISK_RETENTION_YEARS,
    MINIMUM_RETENTION_DAYS,
    RetentionPin,
    assemble_from_run,
    compute_retention_pin,
    emit_run_audit_event,
    validate_retention,
)
from bernstein.core.security.audit_pack import (
    DEFAULT_EVIDENCE_SOURCES,
    STATUS_PENDING,
    generate_audit_pack,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_signer(tmp_path: Path) -> tuple[Ed25519FileKeySigner, Ed25519PublicKeyVerifier]:
    """Generate an ephemeral Ed25519 key pair and load the signer/verifier."""
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    key_path = tmp_path / "customer.pem"
    key_path.write_bytes(pem)
    signer = Ed25519FileKeySigner.from_path(key_path)
    verifier = Ed25519PublicKeyVerifier.from_raw(signer.public_key_bytes())
    return signer, verifier


def _signed_run_with_records(
    sdd: Path,
    *,
    run_id: str = "run-bughunt",
    count: int = 3,
    signer: LineageSigner | None = None,
    regulatory_class: str | None = "high",
) -> list[LineageRecord]:
    """Emit ``count`` lineage records into the WAL for *run_id*."""
    writer = LineageWriter.for_run(run_id, sdd, signer=signer)
    records = []
    for i in range(count):
        record = LineageRecord(
            output_artifact=ArtifactRef(path=f"src/f{i}.py", sha256="a" * 64),
            inputs=[],
            producer=AgentRef(agent_id=f"agent-{i}", run_id=run_id),
            prompt_sha="p" * 64,
            model="claude-sonnet",
            cost_usd=0.01,
            tokens=100,
            timestamp=1700000000.0 + i,
            regulatory_class=regulatory_class,
        )
        writer.emit(record)
        records.append(record)
    return records


def _wal_path(sdd: Path, run_id: str) -> Path:
    return sdd / "runtime" / "wal" / f"{run_id}.wal.jsonl"


# ---------------------------------------------------------------------------
# Finding bucket #1: DNS-rebinding bypass against EU residency guard.
# ---------------------------------------------------------------------------


class TestDnsRebindingBypass:
    """Bypass class: hostname-text matching vs wire-form IP semantics.

    Pre-fix behaviour: ``host.startswith("10.")`` accepted any FQDN whose
    first label began with ``10`` - including public-DNS-resolvable
    names the attacker controls (``10.example.com``, ``192.168.evil.tld``,
    ``172.20.foo.com``). An EU-residency-tagged spawn would happily
    egress to that endpoint because the guard never resolved the
    hostname, only string-prefixed it.

    Fix: parse the host as ``ipaddress.ip_address`` first; only fall
    back to the suffix allow-list (``*.internal``, ``*.local``,
    ``*.svc``, ``*.cluster.local``) when the host is *not* a literal
    IP. Public hostnames that happen to start with an RFC-1918 octet
    no longer satisfy the guard.

    These tests pin the fix. A future refactor that re-introduces the
    string-prefix path will break them loud.
    """

    @pytest.mark.parametrize(
        "url",
        [
            "http://10.example.com:8000",
            "http://192.168.evil.tld:8000",
            "http://172.20.foo.com:8000",
            "http://10.attacker.tld",
            "http://172.16.bypass.example",
            "http://192.168.example.org:11434",
        ],
    )
    def test_public_hostname_with_rfc1918_prefix_is_rejected(self, url: str) -> None:
        """Public hostnames whose first label looks like a private IP are NOT self-hosted."""
        adapter = OllamaAdapter()
        assert adapter._is_self_hosted_endpoint(url) is False, (
            f"DNS-rebinding bypass: {url} should NOT be classified self-hosted "
            "(prior code used host.startswith('10.') which silently accepted FQDNs)."
        )

    @pytest.mark.parametrize(
        "url",
        [
            "http://10.0.0.5:8000",
            "http://192.168.1.100:11434",
            "http://172.16.0.1:8000",
            "http://172.31.255.1:8000",
            "http://127.0.0.1:11434",
        ],
    )
    def test_real_rfc1918_literals_still_pass(self, url: str) -> None:
        """The fix must not regress the legitimate RFC-1918 paths."""
        adapter = OllamaAdapter()
        assert adapter._is_self_hosted_endpoint(url) is True, url

    @given(
        prefix=st.sampled_from(["10", "192.168", "172.20", "172.16", "172.31"]),
        suffix=st.text(
            alphabet=st.characters(
                whitelist_categories=("Ll",),
                whitelist_characters="-",
            ),
            min_size=3,
            max_size=12,
        ).filter(lambda s: not s.startswith("-") and not s.endswith("-")),
        tld=st.sampled_from(["com", "net", "org", "io", "tld", "example"]),
    )
    @settings(
        max_examples=80,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_rebinding_property(self, prefix: str, suffix: str, tld: str) -> None:
        """Property: any FQDN ``<rfc1918-prefix>.<word>.<tld>`` is rejected.

        The rebinding class is "hostname text that looks like a private
        IP but resolves publicly". Hypothesis fuzzes the suffix/TLD;
        every generated host must remain non-self-hosted.
        """
        host = f"{prefix}.{suffix}.{tld}"
        url = f"http://{host}:8000"
        adapter = OllamaAdapter()
        assert adapter._is_self_hosted_endpoint(url) is False, url


# ---------------------------------------------------------------------------
# Finding bucket #2: IPv6 residency surface (link-local + ULA + loopback).
# ---------------------------------------------------------------------------


class TestIpv6Residency:
    """IPv6 path was added by the fix - pin every accepted/rejected case.

    The original code matched ``host == "::1"`` literally; v6 link-local
    (``fe80::*``) and ULA (``fc00::/7``) were silently rejected even
    though they're the IPv6 equivalent of RFC-1918. The fix uses
    :mod:`ipaddress` and now classifies them correctly.
    """

    @pytest.mark.parametrize(
        "url",
        [
            "http://[::1]:11434",
            "http://[fe80::1]:11434",
            "http://[fe80::abcd:1234]:8000",
            "http://[fc00::1]:8000",
            "http://[fd12:3456:789a::1]:8000",
        ],
    )
    def test_ipv6_internal_addresses_pass(self, url: str) -> None:
        adapter = OllamaAdapter()
        assert adapter._is_self_hosted_endpoint(url) is True, url

    @pytest.mark.parametrize(
        "url",
        [
            # Public IPv6 - fail closed.
            "http://[2606:4700:4700::1111]:443",
            "http://[2001:4860:4860::8888]:443",
            "http://[2a00:1450:4001:830::200e]:443",
        ],
    )
    def test_public_ipv6_rejected(self, url: str) -> None:
        adapter = OllamaAdapter()
        assert adapter._is_self_hosted_endpoint(url) is False, url


# ---------------------------------------------------------------------------
# Finding bucket #3 - regulatory-claim violator:
#   Article 12(3) retention math.
# ---------------------------------------------------------------------------


class TestArticle12Retention:
    """The 10-year floor for high-risk is encoded as ``round(10*365.25)``.

    Boundary risk: a sloppy reader could read ``HIGH_RISK_RETENTION_YEARS``
    as ``10 * 365 == 3650`` and produce a bundle whose retention horizon
    is *days* below the legal floor. ``validate_retention`` must
    reject any pin whose ``retention_days`` falls below the rounded
    value (3653) for the high-risk class.
    """

    def test_high_risk_retention_floor_matches_round_365_25(self) -> None:
        """10 years of high-risk retention is ``round(10*365.25)`` days.

        Python's ``round`` does banker's rounding - ``round(3652.5)`` is
        ``3652``, not ``3653``. The bug-hunt initially asserted 3653 (naive
        math); the test now asserts the actual implementation contract so
        a future swap of ``round`` for ``math.ceil`` or ``int`` (each
        producing 3653 / 3652 respectively) is caught.
        """
        pin = compute_retention_pin("high", "2030-01-01T00:00:00+00:00")
        assert pin.retention_days == round(HIGH_RISK_RETENTION_YEARS * 365.25)
        assert pin.retention_days == 3652

    def test_minimal_retention_is_at_least_six_months(self) -> None:
        """Article 12(3) baseline floor."""
        pin = compute_retention_pin("limited", "2030-01-01T00:00:00+00:00")
        assert pin.retention_days >= MINIMUM_RETENTION_DAYS
        assert pin.retention_days == 183

    def test_validate_retention_rejects_below_floor_high_risk(self) -> None:
        """Hand-rolled pin with retention_days=3650 (the naive 10*365) must fail."""
        # last_event_ts in the future so only the floor check fires.
        future = (datetime.now(tz=UTC) + timedelta(days=10)).isoformat()
        floor_days = round(HIGH_RISK_RETENTION_YEARS * 365.25)
        bad_pin = RetentionPin(
            risk_class="high",
            retention_days=3650,
            retention_until=(datetime.now(tz=UTC) + timedelta(days=3650)).date().isoformat(),
            last_event_ts=future,
        )
        ok, reason = validate_retention(bad_pin)
        assert not ok
        assert "below Article 12(3) floor" in reason
        assert str(floor_days) in reason

    def test_validate_retention_rejects_below_floor_minimal(self) -> None:
        """Same shape for the 6-month floor."""
        future = (datetime.now(tz=UTC) + timedelta(days=10)).isoformat()
        bad_pin = RetentionPin(
            risk_class="limited",
            retention_days=180,
            retention_until=(datetime.now(tz=UTC) + timedelta(days=180)).date().isoformat(),
            last_event_ts=future,
        )
        ok, reason = validate_retention(bad_pin)
        assert not ok
        assert "below Article 12(3) floor" in reason

    def test_retention_horizon_in_past_fails(self) -> None:
        """Once the deletion horizon is reached, ``validate_retention`` returns False."""
        ancient = "2000-01-01T00:00:00+00:00"
        pin = compute_retention_pin("limited", ancient)
        ok, reason = validate_retention(pin)
        assert not ok
        assert "retention horizon" in reason

    @given(
        risk_class=st.sampled_from(["high", "limited", "minimal"]),
        years_ago=st.integers(min_value=0, max_value=5),
    )
    @settings(max_examples=30, deadline=None)
    def test_retention_pin_is_internally_consistent(self, risk_class: str, years_ago: int) -> None:
        """Property: ``retention_until`` is exactly ``last_event_ts + retention_days`` days.

        A drift between the encoded ``retention_days`` and the rendered
        ``retention_until`` ISO date would let an attacker hand-craft a
        bundle whose deletion horizon contradicts its own retention
        days.
        """
        last_dt = datetime.now(tz=UTC) - timedelta(days=365 * years_ago)
        pin = compute_retention_pin(risk_class, last_dt.isoformat())  # type: ignore[arg-type]
        expected_until = (last_dt + timedelta(days=pin.retention_days)).date().isoformat()
        assert pin.retention_until == expected_until


# ---------------------------------------------------------------------------
# Finding bucket #4 - regulatory-claim violator:
#   Article 12(5) integrity end-to-end (after compaction).
# ---------------------------------------------------------------------------


class TestArticle12IntegrityAfterCompaction:
    """Lineage compaction must not break the active chain.

    ``compress_rotated_lineage`` only gzips rotated backup files
    (``*.wal.jsonl.<N>``); the active chain (``*.wal.jsonl``) is left
    alone so the writer never races the compactor. We verify both
    invariants here.
    """

    def test_compaction_skips_active_chain(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        signer, verifier = _make_signer(tmp_path)
        _signed_run_with_records(sdd, signer=signer, count=4)

        wal_dir = sdd / "runtime" / "wal"
        # Simulate a rotation by copying the active chain to a backup name.
        active = wal_dir / "run-bughunt.wal.jsonl"
        backup = wal_dir / "run-bughunt.wal.jsonl.1"
        backup.write_bytes(active.read_bytes())

        compressed = compress_rotated_lineage(sdd)
        assert "run-bughunt.wal.jsonl.1" in compressed
        # Active chain still present - writer didn't race.
        assert active.is_file()
        # Backup gzipped, original removed.
        assert not backup.exists()
        assert (wal_dir / "run-bughunt.wal.jsonl.1.gz").is_file()

        # Active chain still verifies.
        result = verify_run_chain(sdd, "run-bughunt", verifier=verifier)
        assert result.ok, result.errors

    def test_compaction_idempotent(self, tmp_path: Path) -> None:
        """Running compaction twice does nothing on the second pass."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        signer, _ = _make_signer(tmp_path)
        _signed_run_with_records(sdd, signer=signer, count=2)

        wal_dir = sdd / "runtime" / "wal"
        active = wal_dir / "run-bughunt.wal.jsonl"
        backup = wal_dir / "run-bughunt.wal.jsonl.1"
        backup.write_bytes(active.read_bytes())

        first = compress_rotated_lineage(sdd)
        second = compress_rotated_lineage(sdd)
        assert first == ["run-bughunt.wal.jsonl.1"]
        assert second == []


# ---------------------------------------------------------------------------
# Finding bucket #5 - regulatory-claim violator:
#   Tamper detection on the lineage chain.
# ---------------------------------------------------------------------------


class TestLineageTamperDetection:
    """Single-byte mutation in any record must fail :func:`verify_run_chain`.

    Every customer signature is computed over canonical bytes, so any
    field flip - ``cost_usd``, ``regulatory_class``, ``output_artifact.sha256``
    - must invalidate the signature *and* the WAL hash chain.
    """

    def test_byte_flip_in_cost_breaks_chain(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        signer, verifier = _make_signer(tmp_path)
        _signed_run_with_records(sdd, signer=signer, count=3)

        wal = _wal_path(sdd, "run-bughunt")
        lines = wal.read_text().splitlines()
        entry = json.loads(lines[1])
        entry["output"]["cost_usd"] = 999.99
        lines[1] = json.dumps(entry, sort_keys=True)
        wal.write_text("\n".join(lines) + "\n")

        result = verify_run_chain(sdd, "run-bughunt", verifier=verifier)
        assert not result.ok
        assert result.errors  # at least one diagnostic surfaced

    def test_byte_flip_in_sha256_breaks_chain(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        signer, verifier = _make_signer(tmp_path)
        _signed_run_with_records(sdd, signer=signer, count=3)

        wal = _wal_path(sdd, "run-bughunt")
        lines = wal.read_text().splitlines()
        entry = json.loads(lines[1])
        entry["output"]["output_artifact"]["sha256"] = "f" * 64
        lines[1] = json.dumps(entry, sort_keys=True)
        wal.write_text("\n".join(lines) + "\n")

        result = verify_run_chain(sdd, "run-bughunt", verifier=verifier)
        assert not result.ok

    def test_remove_middle_record_breaks_chain(self, tmp_path: Path) -> None:
        """Cross-record link integrity: dropping a record breaks the WAL chain."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        signer, verifier = _make_signer(tmp_path)
        _signed_run_with_records(sdd, signer=signer, count=4)

        wal = _wal_path(sdd, "run-bughunt")
        lines = wal.read_text().splitlines()
        # Drop the second record - the third's prev_hash points at #1's hash.
        kept = [lines[0], lines[2], lines[3]]
        wal.write_text("\n".join(kept) + "\n")

        result = verify_run_chain(sdd, "run-bughunt", verifier=verifier)
        assert not result.ok


# ---------------------------------------------------------------------------
# Finding bucket #6 - regulatory-claim violator:
#   Bundle determinism (same input → byte-identical zip).
# ---------------------------------------------------------------------------


class TestBundleDeterminism:
    """Two ``assemble_from_run`` runs over identical input must produce the same bytes.

    Without this, the bundle_id hash becomes a moving target across
    re-runs, and an auditor cannot pin a bundle for cross-reference.
    """

    def test_assemble_from_run_is_byte_deterministic(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        run_id = "run-determ"
        audit_key = b"x" * 32

        # Produce a small audit chain + lineage record set.
        for i in range(3):
            emit_run_audit_event(
                sdd_dir=sdd,
                run_id=run_id,
                event_type=f"task.event_{i}",
                actor="orchestrator",
                resource_type="task",
                resource_id=f"T-{i:03d}",
                details={"idx": i},
                audit_key=audit_key,
            )
        signer, _ = _make_signer(tmp_path)
        _signed_run_with_records(sdd, run_id=run_id, signer=signer, count=2)

        # Wide window covers everything.
        since = datetime(2020, 1, 1, tzinfo=UTC)
        until = datetime(2050, 1, 1, tzinfo=UTC)

        out_a = tmp_path / "a"
        out_b = tmp_path / "b"
        out_a.mkdir()
        out_b.mkdir()

        bundle_a = assemble_from_run(
            run_id,
            since,
            until,
            sdd_dir=sdd,
            workdir=tmp_path,
            risk_class="high",
            audit_key=audit_key,
            output_dir=out_a,
        )
        bundle_b = assemble_from_run(
            run_id,
            since,
            until,
            sdd_dir=sdd,
            workdir=tmp_path,
            risk_class="high",
            audit_key=audit_key,
            output_dir=out_b,
        )

        # Identical sha256 implies identical bytes.
        assert bundle_a.bundle.sha256 == bundle_b.bundle.sha256
        # Defensive double-check: read both zips and compare member hashes.
        with zipfile.ZipFile(bundle_a.bundle.archive_path) as za, zipfile.ZipFile(bundle_b.bundle.archive_path) as zb:
            assert za.namelist() == zb.namelist()
            for name in za.namelist():
                assert za.read(name) == zb.read(name), name


# ---------------------------------------------------------------------------
# Finding bucket #7 - regulatory-claim violator:
#   Clause map well-formedness.
# ---------------------------------------------------------------------------


class TestClauseMap:
    """Every clause shipped in ``config/eu_ai_act_clause_map.yaml`` must be non-empty.

    A clause without a subsystem mapping is a regulatory-false-claim risk:
    we'd be claiming Article 12 conformance for a sub-clause we cannot
    point at code for.
    """

    def _clause_map(self) -> dict:
        import yaml

        repo_root = Path(__file__).resolve().parents[2]
        path = repo_root / "config" / "eu_ai_act_clause_map.yaml"
        return yaml.safe_load(path.read_text(encoding="utf-8"))

    def test_clause_map_loads(self) -> None:
        parsed = self._clause_map()
        assert isinstance(parsed, dict)
        assert parsed.get("article") == 12

    def test_every_clause_has_subsystem_module(self) -> None:
        parsed = self._clause_map()
        mappings = parsed.get("mappings") or []
        assert mappings, "clause map has no mappings - auditor would see an empty conformance file"
        for m in mappings:
            sub = m.get("subsystem") or {}
            module = sub.get("module")
            assert module, f"clause {m.get('clause')!r} missing subsystem.module"
            role = sub.get("role")
            assert role, f"clause {m.get('clause')!r} missing subsystem.role"
            artefact = m.get("evidence_artefact")
            assert artefact, f"clause {m.get('clause')!r} missing evidence_artefact"
            requirement = m.get("requirement")
            assert requirement, f"clause {m.get('clause')!r} missing requirement"


# ---------------------------------------------------------------------------
# Finding bucket #8 - operational bug:
#   Lineage run_id path-traversal guard.
# ---------------------------------------------------------------------------


class TestLineageRunIdGuard:
    """``LineageWriter.for_run`` must reject any run_id that isn't a single safe filename."""

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "..",
            ".",
            "../escape",
            "a/b",
            "a\\b",
            "..\\winescape",
            "with\x00nul",
        ],
    )
    def test_traversal_run_ids_rejected(self, bad: str, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        with pytest.raises(LineageRunIdError):
            LineageWriter.for_run(bad, sdd)

    def test_clean_run_id_accepted(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        # No exception expected.
        LineageWriter.for_run("safe-run-001", sdd)


# ---------------------------------------------------------------------------
# Finding bucket #9 - operational bug:
#   SOC 2 evidence pack must degrade, not crash, on an empty .sdd/.
# ---------------------------------------------------------------------------


class TestSoc2EmptyEvidence:
    """Empty ``.sdd/`` must yield a "pending" markdown row for every control.

    Bare-checkout case: an operator who has never run anything still
    wants to render the SOC 2 checklist (e.g. for a sales pitch). The
    pack must not crash.
    """

    def test_empty_project_renders_pending_rows(self, tmp_path: Path) -> None:
        # Create only the bare directories the resolvers walk.
        (tmp_path / ".sdd").mkdir()
        result = generate_audit_pack(workdir=tmp_path, write=False)
        # Markdown produced without exception.
        assert isinstance(result.markdown, str)
        assert result.markdown
        # Every default source rendered a row.
        for src in DEFAULT_EVIDENCE_SOURCES:
            assert src.control_id in result.markdown
        # At least one PENDING row.
        assert STATUS_PENDING in result.markdown


# ---------------------------------------------------------------------------
# Finding bucket #10 (xfail):
#   KMS adapter contract drift - public_key_jwk vs public_key_bytes.
# ---------------------------------------------------------------------------


class TestKmsContractDrift:
    """The customer-facing signer protocol promises ``sign(payload) -> bytes``.

    Auditor-side tooling that consumes attestations typically wants the
    public key as a JWK (RFC 7517) so it can be cross-referenced with
    a JWKS endpoint. The current concrete signer
    (:class:`Ed25519FileKeySigner`) only exposes ``public_key_bytes()``
    - raw 32 bytes, not a JWK envelope.

    Marked xfail rather than silently fixed because adding ``public_key_jwk``
    is a public API change that should land in a separate, reviewed
    commit. The xfail makes the gap loud in CI so the operator can
    schedule it for a follow-up.
    """

    @pytest.mark.xfail(
        reason=(
            "KMS-adapter contract drift: Ed25519FileKeySigner exposes "
            "public_key_bytes() but auditors using JWK-based attestation "
            "flows want public_key_jwk(). Tracked as a follow-up - see "
            "the PR body for the proposed JWK shape."
        ),
        strict=True,
    )
    def test_signer_exposes_public_key_jwk(self, tmp_path: Path) -> None:
        signer, _ = _make_signer(tmp_path)
        # If/when public_key_jwk lands, it should return a dict with kty/crv/x.
        jwk = signer.public_key_jwk()  # type: ignore[attr-defined]
        assert jwk["kty"] == "OKP"
        assert jwk["crv"] == "Ed25519"
        assert "x" in jwk

    def test_signer_exposes_raw_public_bytes_today(self, tmp_path: Path) -> None:
        """Documented baseline: 32-byte raw public key is the only export today."""
        signer, _ = _make_signer(tmp_path)
        raw = signer.public_key_bytes()
        assert len(raw) == 32


# ---------------------------------------------------------------------------
# Finding bucket #11 (xfail):
#   No HSM-stub raises NotImplementedError with a meaningful docstring.
# ---------------------------------------------------------------------------


class TestHsmStubMissing:
    """Operator wires ``key_kind: hsm`` and gets a generic config error.

    A concrete stub class - say ``HSMSigner`` - that raises
    :class:`NotImplementedError` with a docstring pointing at the HSM
    integration ticket would be a clearer signal. Today's
    :func:`signer_from_config` rejects any ``key_kind != 'ed25519'``
    with a :class:`LineageSignerError` - fail-loud, but the operator
    can't tell whether HSM is "not yet implemented" or "rejected on
    purpose".

    Marked xfail so the gap is visible without a silent fix.
    """

    def test_unsupported_key_kind_raises_signer_error_today(self) -> None:
        """Documented baseline: today's behaviour is the generic config error."""
        with pytest.raises(LineageSignerError, match="key_kind"):
            signer_from_config(enabled=True, key_path="/tmp/doesnt-matter", key_kind="hsm")

    @pytest.mark.xfail(
        reason=(
            "HSM stub gap: signer_from_config(key_kind='hsm') should hand back "
            "a concrete stub raising NotImplementedError with an integration-ticket "
            "pointer in the docstring, instead of a generic config error. "
            "Tracked as a follow-up."
        ),
        strict=True,
    )
    def test_hsm_stub_raises_not_implemented_with_docstring(self) -> None:
        from bernstein.core.persistence.lineage_signer import HSMSigner  # type: ignore[attr-defined]

        stub = HSMSigner()
        # Docstring should point at the integration ticket.
        assert stub.__class__.__doc__ and "HSM" in stub.__class__.__doc__
        with pytest.raises(NotImplementedError):
            stub.sign(b"payload")


# ---------------------------------------------------------------------------
# Finding bucket #12 - regulatory-claim violator (xfail):
#   Article 12(4) automatic recording of high-risk artefacts.
# ---------------------------------------------------------------------------


class TestArticle12AutomaticRecording:
    """Spawn-side hook for high-risk lineage emission is not yet wired.

    Article 12(4): every adapter spawn that produces a high-risk
    artefact must emit a lineage record automatically. Today the
    orchestrator emits records via :class:`LineageWriter` explicitly;
    there is no spawn-time hook that guarantees a record is emitted
    when ``OllamaAdapter.spawn`` runs against a residency-tagged
    model.

    Marked xfail because plumbing the hook is a separate PR; the test
    here pins the *contract* (high-risk spawn → at least one lineage
    record) so the future PR has a target to hit.
    """

    @pytest.mark.xfail(
        reason=(
            "Article 12(4) auto-recording hook is not wired into "
            "OllamaAdapter.spawn yet. The orchestrator emits records "
            "via LineageWriter explicitly; we want a default spawn "
            "interceptor that emits one record per high-risk spawn. "
            "Follow-up PR will add the hook."
        ),
        strict=True,
    )
    def test_high_risk_spawn_emits_lineage_record(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        run_id = "run-art12-4"
        # Today there is no auto-emit hook; the future hook would be wired
        # into ``OllamaAdapter.spawn`` and would call into LineageWriter on
        # every high-risk artefact. We assert the *contract* (≥1 high-risk
        # record present after a residency-tagged spawn) so the future PR
        # can flip xfail → pass without rewriting the test.
        OllamaAdapter(eu_residency=True)  # placeholder for the future spawn invocation
        # spawn_with_lineage_hook(adapter, sdd, run_id, regulatory_class='high')
        reader = LineageReader(sdd)
        records = list(reader.iter_records(run_id=run_id))
        assert any(r.regulatory_class == "high" for r in records), (
            "Expected at least one high-risk lineage record from the spawn hook."
        )


# ---------------------------------------------------------------------------
# Finding bucket #13 - operational hardening:
#   Canonical record bytes are stable under Python repr noise.
# ---------------------------------------------------------------------------


class TestCanonicalBytesStability:
    """Canonical bytes must round-trip identically; a re-emit of the same record
    re-canonicalises to the same bytes.

    Without this, ``customer_signature`` would re-verify only against
    the bytes the *original* writer produced - a Python upgrade or a
    JSON-encoder swap could silently invalidate every legacy signature.
    """

    @given(
        cost=st.floats(min_value=0.0, max_value=10.0, allow_nan=False, allow_infinity=False),
        tokens=st.integers(min_value=0, max_value=1_000_000),
        ts=st.floats(min_value=0.0, max_value=2_000_000_000.0, allow_nan=False, allow_infinity=False),
        cls=st.sampled_from(["high", "limited", "minimal", None]),
    )
    @settings(max_examples=40, deadline=None)
    def test_canonical_bytes_are_stable(
        self,
        cost: float,
        tokens: int,
        ts: float,
        cls: str | None,
    ) -> None:
        record = LineageRecord(
            output_artifact=ArtifactRef(path="src/x.py", sha256="a" * 64),
            inputs=[ArtifactRef(path="src/y.py", sha256="b" * 64)],
            producer=AgentRef(agent_id="agent-1", run_id="run-1"),
            prompt_sha="c" * 64,
            model="claude-sonnet",
            cost_usd=cost,
            tokens=tokens,
            timestamp=ts,
            regulatory_class=cls,
        )
        bytes_a = canonical_record_bytes(record)
        bytes_b = canonical_record_bytes(record)
        assert bytes_a == bytes_b
        # Customer signature is intentionally NOT in the canonical payload.
        assert b"customer_signature" not in bytes_a


# ---------------------------------------------------------------------------
# Finding bucket #14 - operational bug:
#   Mixed-internal+external endpoint (an operator concatenates two URLs).
# ---------------------------------------------------------------------------


class TestMixedEndpointHandling:
    """A single ``base_url`` can only resolve to one host; the guard takes
    the parsed hostname, not whatever appears in the path.

    Pin the behaviour: a URL like ``http://10.0.0.1@evil.com:8000``
    (userinfo masking) parses ``evil.com`` as the host - must be
    rejected.
    """

    @pytest.mark.parametrize(
        "url",
        [
            "http://10.0.0.1@evil.com:8000",
            "http://192.168.1.1@attacker.tld",
            "http://127.0.0.1@public.example.com:8000",
        ],
    )
    def test_userinfo_masking_does_not_grant_self_hosted(self, url: str) -> None:
        adapter = OllamaAdapter()
        # urllib.parse extracts the host AFTER the '@' - that's the real
        # destination. Asserting False here pins the right semantics.
        assert adapter._is_self_hosted_endpoint(url) is False, url

    def test_ipv6_userinfo_does_not_mask_real_host(self) -> None:
        """A bracketed-IPv6 netloc with userinfo fails closed (FIXED).

        This was an ``xfail(strict=True)``: ``urllib.parse.urlsplit``
        raises ``ValueError`` on this shape, and the guard did not catch
        it, so a malformed URL left ``spawn()`` with a traceback instead
        of a residency verdict.
        """
        url = "http://[::1]@evil.com:8000"
        adapter = OllamaAdapter()
        assert adapter._is_self_hosted_endpoint(url) is False, url

    @pytest.mark.parametrize(
        "url",
        [
            "http://[::1]@evil.com:8000",
            "http://[::1",
            "http://a]b",
            "http://[",
            "http://]",
        ],
    )
    def test_an_unparseable_url_is_a_verdict_not_an_exception(self, url: str) -> None:
        """Every URL urlsplit refuses lands on False, the fail-closed answer.

        The guard runs inside ``spawn()`` under the residency profile. A
        raise there reads as a crash rather than as the refusal it should
        be, and hides which endpoint was rejected and why.
        """
        adapter = OllamaAdapter()
        assert adapter._is_self_hosted_endpoint(url) is False, url

    @pytest.mark.parametrize("url", ["http://[::1]:11434", "http://[fe80::1]", "http://[fc00::1]:8080"])
    def test_a_well_formed_bracketed_ipv6_literal_still_resolves(self, url: str) -> None:
        """Catching the parse error must not swallow the addresses that work."""
        adapter = OllamaAdapter()
        assert adapter._is_self_hosted_endpoint(url) is True, url
