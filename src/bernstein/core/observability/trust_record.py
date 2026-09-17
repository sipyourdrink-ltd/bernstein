"""Trust Record emitter for TRACE 0.2 format.

This module provides a deterministic emitter that constructs a TRACE 0.2
compliant Trust Record from a journal path, signs it with the install
Ed25519 identity, and returns the canonical JSON.

This is signed software evidence, not hardware attestation: the producer
has no TEE, no TPM, and no hardware root of trust. Every claim below is
derived from the run journal and the install's Ed25519 key alone.

Seal boundary: the signed seal proves the journal presented matches what
was sealed at signing time; it cannot prove that every action taken during
the run was recorded to the journal in the first place.

Field surface (re-aligned to the upstream schema at agentrust-io/trace-spec
pin e7e2eca, following the producer-mapping review of
agentrust-io/trace-spec#231, issue #4760/#4761/#4762)::

    {
      "eat_profile": "tag:agentrust-io.com,2026:trace-v0.2",
      "iat": <int, unix seconds>,
      "subject": "<spiffe:// URI, execution-scoped>",
      "model": {"provider": "<str>", "model_id": "<str>", "version": "<str>"?},
      "runtime": {"platform": "software-only", "measurement": "<all-zero sha256>"},
      "policy": {"bundle_hash": "<sha256 of the resolved gate config>", "enforcement_mode": "enforce"},
      "data_class": "<str>",
      "tool_transcript": {"hash": "<sha256 over tool-call entries>", "call_count": <int>},
      "build_provenance": {"slsa_level": 0, "digest": "<sha256 hex>", "provenance_uri": "<url>"},
      "appraisal": {"status": "none", "verifier": "<fixed URI>", "timestamp": <int>},
      "cnf": {"jwk": {"kty": "OKP", "crv": "Ed25519", "x": "<base64url>", "kid": "<key-id>"}},
      "delegation": {"parent_record_hash": "sha256:<64 hex>", "credential_id": "<str>"},  // child hops only
      "references": [{"rel": "produced-artifact", "id": "<str>", "resolver": "<str>", "digest": "<str>"}],
      // references is only present when non-empty
      "signature": "<base64url, no padding>"
    }

``delegation`` and ``references`` are present only when non-trivial: a root
or solo execution carries no ``delegation`` member at all (not a null
``parent`` -- the previous ``{"parent": null}`` shape is gone), and
``references`` is omitted rather than emitted as an empty list when the
execution produced no artifacts. This mirrors how ``tool_transcript``
replaced the old journal-pointer ``references[rel=evidence]`` entry: the
tool-call digest *is* the journal evidence for an execution record now, so
no execution record ever carries a ``rel: "evidence"`` reference. The
``member-execution`` relation is carried only by the run-level aggregate
record (see below), never by an execution record.

Subject scheme: a fixed-trust-domain SPIFFE URI,
``spiffe://bernstein.run/run/<run_id>/exec/<exec_id>``. Unlike the
previous install-key-derived trust domain, ``bernstein.run`` is a literal
constant: two installs signing the same ``(run_id, exec_id)`` pair mint the
same subject (they still sign with different keys, so ``cnf.jwk`` -- not
``subject`` -- is what a verifier uses to tell installs apart).

``run_id``/``exec_id`` derivation (issue #4761 AC1): these two identifiers
are caller-supplied SPIFFE path segments (see
:func:`_require_spiffe_segment`) -- the emitter itself never reads either
one *from* the journal, and does not check either against the journal's
own content. The contract a caller must uphold, so a verifier can
reproduce it from the journal alone:

- ``exec_id`` names *this one execution's* journal and must be the same
  string that identifies that journal on disk. Every journal built through
  :class:`bernstein.core.replay.journal.EventJournal` already carries this
  identifier as its own :attr:`EventJournal.run_id` property (a
  same-named but narrower concept than this module's ``run_id`` -- see
  below), and the journal path itself encodes it: a production journal
  lives at ``<sdd_dir>/runs/<that-id>/journal.jsonl``
  (:func:`bernstein.core.replay.journal.run_journal_path`), so
  ``journal_path.parent.name`` recovers the expected ``exec_id`` for any
  journal built that way. A verifier can therefore reproduce the
  correspondence without trusting the caller: fetch the journal named in
  ``journal_path.parent.name`` and confirm it matches the ``exec_id``
  embedded in the record's ``subject``.
- ``run_id`` is the *wider*, multi-hop grouping identifier: every
  execution hop of one delegated run must be called with the same
  ``run_id`` (this is what lets :meth:`TrustRecordEmitter.emit_aggregate_trust_record`
  roll several executions' records up into one run-scoped record). No
  single journal carries this value -- it is not any one
  :class:`EventJournal`'s own ``run_id`` property, despite the name
  collision with that unrelated, per-journal identifier. Today, minting
  every hop's ``run_id`` consistently is the caller's responsibility (the
  real caller -- the ``bernstein trace export`` CLI, issue #4667 -- is
  scoped out of this module); this module only validates its *shape* (a
  safe SPIFFE segment), never its consistency across hops.

Journal conventions this emitter reads (all producer-owned; no other
module depends on these key names today, so this docstring is their only
specification):

- ``model_id`` / ``model_provider`` / ``model_version`` (optional) on any
  event: the *last* event carrying ``model_id`` wins, so a mid-run model
  switch is reflected honestly. Required -- ``model`` is a required TRACE
  member and there is no honest default for it.
- ``gate_config`` on any event: the *last* such event's value is the
  resolved policy/gate configuration this execution ran under.
  ``policy.bundle_hash`` is ``sha256:`` + the hex digest of its JCS
  (RFC 8785) canonicalisation. Required for the same reason as ``model``.
- ``data_class`` on any event: the *last* such value wins. Optional --
  defaults to the conservative :data:`_DEFAULT_DATA_CLASS` when the
  operator declared none.
- Events with ``event == "tool_call"``: every one is folded, in journal
  order, into ``tool_transcript`` (hash over the JCS canonicalisation of
  the ordered list of their payloads, plus a ``call_count``). Always
  present, even at zero calls -- "no tool calls happened" is a fact, not a
  hole to leave out the way an unknown timestamp is.
- Events with ``event == "artifact_produced"`` carrying ``artifact_id``,
  ``resolver``, and ``digest``: each becomes one
  ``references[]`` entry with ``rel: "produced-artifact"``.

``iat`` and ``appraisal.timestamp`` both come from the *last* event's
wall-clock ``ts`` (rounded to the nearest whole second), i.e. execution
completion time. A journal with no events has no completion time and
cannot back a Trust Record, so an empty journal is refused the same way a
broken chain is.

Delegation: a delegated multi-agent run emits one Trust Record per
execution hop rather than nesting them. A child hop's record carries
``delegation``, keyed on the parent hop's own canonical signed record
(pass the parent's ``emit_trust_record`` return value back in as
``parent_record=``, and the delegation credential id as
``credential_id=``); ``delegation.parent_record_hash`` is
``sha256:`` + the hex SHA-256 of the JCS (RFC 8785) canonical form of
the complete signed parent record (including ``signature``). A root
execution has no ``delegation`` member at all.

Signature envelope (re-aligned to the schema, issues #4760-#4762 STEP 0):
the schema's ``signature`` member is an OPTIONAL plain base64url string --
"a signature ... by the cnf key over the canonical JSON form of the record
with only this field absent" -- not an object. The pre-#4760 code emitted
a JWS-header-shaped object (``{"alg", "kid", "sig"}``) there, which is a
*type* violation of the schema (a string-typed property holding an
object), not merely a stylistic mismatch, since the schema also sets
``additionalProperties: false``. This module now emits exactly what the
schema describes: a bare Ed25519 signature (no JOSE header, no detached-JWS
framing) over the JCS (RFC 8785) canonicalisation of every other top-level
field. The key id that used to live in the signature object now lives in
``cnf.jwk.kid`` instead -- an explicitly schema-legal JWK member, and the
right place for it: a key id is a property of the key, not of one
signature the key happens to have produced.

Signing pre-image, corrected (issue #4764 conformance harness, mapping-vs-
implementation conflict #2): "the canonical JSON form of the record with
only [signature] absent" means exactly that -- the record *as it will be
serialized*, nothing more. The interim STEP 0 code (and the fixture vectors
it minted) instead built the pre-image from a fixed field list that always
included ``delegation`` and ``references``, substituting an explicit
``null`` when a record omits either. Running the vendored fixtures through
the reference ``agentrust-trace-tests`` executable conformance suite
(``trace-tests verify``) surfaced this directly: TR-SIG-005 failed on every
record, because its own canonicalisation -- ``{k: v for k, v in
record.items() if k != "signature"}`` -- has no key for a member the record
does not carry, and RFC 8785 canonicalises "no key" and "key present with a
``null`` value" to different bytes. This module now builds the pre-image
the same way: :func:`sign_trust_record` and :func:`verify_trust_record`
both sign/verify over exactly the record's own present keys (minus
``signature``), so a root record's pre-image simply has no ``delegation``
entry rather than a ``null`` one. The security property the STEP 0 code was
protecting -- that forging a ``delegation`` onto an unsigned copy of a root
record must not verify -- still holds without the explicit null: adding any
key at all changes the canonicalised bytes, which invalidates the
signature regardless of which convention is used.

The emitter:

- Takes a journal path and reads its events
- Verifies the journal's hash chain before trusting anything it recorded
- Maps journal data to the TRACE 0.2 field surface above
- Signs with the install identity via existing signing infrastructure
- Returns canonical JSON via json.dumps(..., sort_keys=True, separators=(",", ":"))
- Signs over a *different* canonicalisation: the pre-image is JCS (RFC 8785),
  not the returned document. A verifier must re-canonicalise the signed body
  (every field except ``signature`` itself) with JCS rather than hashing
  the bytes it received
- Uses import guards to avoid pulling agentrust_trace when [trace] extra is absent

Aggregate (run-level) records (issue #4763): a delegated run's individual
per-execution records can be rolled up into one further record scoped to
the whole run, minted by :meth:`TrustRecordEmitter.emit_aggregate_trust_record`.
Its ``subject`` is the run-scoped SPIFFE URI (:func:`spiffe_subject_for_aggregate`,
no ``/exec/<id>`` suffix); it carries no ``delegation`` member at all (an
aggregate did not act under anyone's delegated authority -- it is a rollup,
not a hop); and its ``references`` holds one ``{"rel": "member-execution",
"id", "resolver", "digest"}`` entry per member, in the order the members
were given. ``id`` is the member's own ``subject`` -- what names it inside
the resolver -- and ``digest`` is ``sha256:`` + the hex SHA-256 of the
member's complete signed record canonicalised with JCS (RFC 8785) (the same convention
``delegation.parent_record_hash`` uses for a parent record). The digest is
what content-binds the entry: a verifier resolves a member by name and then
recomputes that digest over the record it holds, rather than trusting an
opaque pointer. The aggregate's own ``model``/``policy``/``data_class`` are
rollups over the members (see :meth:`emit_aggregate_trust_record` for the
exact rule for each), not a fresh execution's own values -- there is no
journal backing the aggregate itself.

Public surface:

- :class:`TrustRecordEmitter` -- ``emit_trust_record`` and
  ``emit_aggregate_trust_record`` methods.
- :func:`sign_trust_record` / :func:`verify_trust_record` -- the bare
  sign/verify pair every record (execution or aggregate) is checked
  against; used internally by the emitter and directly by the conformance
  harness (issue #4764).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

__all__ = ["TrustRecordEmitter", "sign_trust_record", "verify_trust_record"]

#: TRACE 0.2 EAT profile identifying this record as a TRACE v0.2 Trust Record
#: (schema ``eat_profile.const``).
_EAT_PROFILE: str = "tag:agentrust-io.com,2026:trace-v0.2"

#: Fixed SPIFFE trust domain for every Trust Record this producer mints.
#: Deliberately a literal, not derived from the install key: the trust
#: domain names the *system*, the signing key (via ``cnf.jwk``) names the
#: *install*.
_SPIFFE_TRUST_DOMAIN: str = "bernstein.run"

#: A run_id/exec_id must be a single safe SPIFFE path segment: no ``/`` (which
#: would let a caller inject extra path components into the subject URI) and
#: non-empty.
_SPIFFE_SEGMENT_RE = re.compile(r"^[^/]+$")

#: ``runtime.measurement`` for a software-only producer: an explicit
#: all-zero digest, never a real measurement. Distinct from an *absent*
#: field -- the schema requires ``measurement``, and an all-zero value is
#: the honest way to say "no hardware measurement exists" rather than
#: overloading a real journal hash (which would look like evidence of a
#: measurement that was never taken).
_ALL_ZERO_SHA256_MEASUREMENT: str = f"sha256:{'0' * 64}"

#: Fixed enforcement mode this producer always asserts: capability-scope and
#: circuit-breaker enforcement are not optional per-run toggles in this
#: codebase, so every policy block says so.
_ENFORCEMENT_MODE: str = "enforce"

#: Conservative default ``data_class`` when the operator declared none at
#: run start. "confidential" rather than "public": an unlabelled run must
#: not default to the least-sensitive classification.
_DEFAULT_DATA_CLASS: str = "confidential"

#: Fixed appraisal verifier URI. Distinct from ``subject`` (which is
#: execution-scoped) -- this identifies the appraisal *method* this producer
#: always uses (self-declared "none"), not the workload being appraised.
_APPRAISAL_VERIFIER_URI: str = "https://bernstein.run/trace/verifier"

#: Release page for ``build_provenance.provenance_uri``. Matches the release
#: URL already used by ``bernstein.cli.release_notes`` and the project's own
#: release announcements.
_RELEASE_PAGE_URI: str = "https://github.com/sipyourdrink-ltd/bernstein/releases"

#: SLSA Build Level this producer claims. 0: software-only, no hermetic /
#: verifiable build pipeline backs the running install.
_SLSA_LEVEL: int = 0

#: Fixed resolver identifier for an aggregate record's ``references[rel=
#: member-execution]`` entries: the party obliged to resolve a member's
#: ``id`` back to the record it names, which the entry's ``digest`` then
#: binds to specific bytes. A literal
#: constant, like ``_APPRAISAL_VERIFIER_URI`` -- this producer always
#: resolves its own member records the same way.
_MEMBER_EXECUTION_RESOLVER_URI: str = "https://bernstein.run/trace/records"


@dataclass(frozen=True, slots=True)
class TrustRecord:
    """TRACE 0.2 Trust Record payload (pre- and post-signature).

    Attributes:
        eat_profile: Constant EAT profile URI for TRACE v0.2.
        iat: Issued-at time (execution completion time, from the journal),
            Unix epoch seconds.
        subject: Execution-scoped SPIFFE URI
            (``spiffe://bernstein.run/run/<run>/exec/<exec>``).
        model: ``{"provider": ..., "model_id": ..., "version": ...?}``.
        runtime: ``{"platform": "software-only", "measurement": <all-zero
            sha256>}``. Software evidence only -- never a hardware
            measurement.
        policy: ``{"bundle_hash": <sha256 of resolved gate config>,
            "enforcement_mode": "enforce"}``.
        data_class: Operator-declared data sensitivity, conservative default
            when undeclared.
        tool_transcript: ``{"hash": <sha256 over tool-call entries>,
            "call_count": <int>}``.
        build_provenance: ``{"slsa_level": 0, "digest": <sha256 hex>,
            "provenance_uri": <url>}``.
        appraisal: ``{"status": "none", "verifier": <fixed URI>,
            "timestamp": <int>}``.
        cnf: ``{"jwk": {"kty": "OKP", "crv": "Ed25519", "x": <base64url>,
            "kid": <key-id>}}``. Public Ed25519 key for key confirmation;
            ``kid`` identifies the install key that produced ``signature``.
        delegation: ``{"parent_record_hash": <sha256:hex>, "credential_id":
            <str>}`` on a delegated child hop; ``None`` for a root/solo
            execution (the member is entirely absent from the output, not
            a null placeholder).
        references: Produced-artifact pointers, or ``None`` when the
            execution produced none (omitted from the output rather than
            emitted as an empty list).
        signature: Base64url (no padding) Ed25519 signature over the JCS
            canonicalisation of every other field, or ``""`` before signing.
    """

    eat_profile: str
    iat: int
    subject: str
    model: dict[str, Any]
    runtime: dict[str, str]
    policy: dict[str, Any]
    data_class: str
    tool_transcript: dict[str, Any]
    build_provenance: dict[str, Any]
    appraisal: dict[str, Any]
    cnf: dict[str, Any]
    delegation: dict[str, Any] | None
    references: list[dict[str, Any]] | None
    signature: str


#: Every top-level field that is *always* part of the signed body -- every
#: TrustRecord carries these regardless of type (execution or aggregate).
#: ``delegation``/``references`` are deliberately NOT in this tuple: they
#: are only added to the signed body when the record actually carries them
#: (see :func:`_record_dict_without_signature`) -- the schema's own words
#: for ``signature`` are "the canonical JSON form of the record with only
#: this field absent", which is the record *as serialized*, not a form
#: padded out with an explicit ``null`` for whichever optional member the
#: record happens to omit. A verifier that adds ``delegation`` to an
#: unsigned copy of a root record still fails to verify it either way: RFC
#: 8785 canonicalises "key present" and "key absent" as different bytes
#: regardless of what value the key would have held.
_BASE_SIGNED_FIELDS: tuple[str, ...] = (
    "eat_profile",
    "iat",
    "subject",
    "model",
    "runtime",
    "policy",
    "data_class",
    "tool_transcript",
    "build_provenance",
    "appraisal",
    "cnf",
)


def _record_dict_without_signature(record: TrustRecord) -> dict[str, Any]:
    """Return the exact JSON object *record* will be signed and emitted as, minus ``signature``.

    The single place both signing (:meth:`TrustRecordEmitter._sign_record`)
    and final-output assembly (:meth:`TrustRecordEmitter.emit_trust_record`,
    :meth:`TrustRecordEmitter.emit_aggregate_trust_record`) build this shape
    from, so the two can never disagree about what the signature covers.
    """
    body: dict[str, Any] = {field: getattr(record, field) for field in _BASE_SIGNED_FIELDS}
    if record.delegation is not None:
        body["delegation"] = record.delegation
    if record.references is not None:
        body["references"] = record.references
    return body


def sign_trust_record(record: dict[str, Any], private_key_pem: bytes) -> dict[str, Any]:
    """Return a copy of *record* with ``signature`` populated.

    *record* must not already carry a ``signature`` key: pass it exactly as
    it will be emitted, with every optional member (``delegation``,
    ``references``) either present with its real value or omitted
    entirely -- never present with an explicit ``null``. Signs the bare
    Ed25519 signature (no JOSE/JWS framing) over the JCS (RFC 8785)
    canonicalisation of *record* itself: nothing is added, removed, or
    substituted before signing, so whatever *record* does or does not
    carry is exactly what gets signed over.

    Pairs with :func:`verify_trust_record`, which reverses this using only
    the parsed record dict and a trusted public key -- no
    :class:`TrustRecord` dataclass or :class:`TrustRecordEmitter` required
    on either side. This is the pair the conformance harness (issue #4764)
    round-trips every vector through, and what
    :meth:`TrustRecordEmitter._sign_record` calls internally.

    Raises:
        ValueError: *record* already has a ``signature`` key.
    """
    if "signature" in record:
        msg = "sign_trust_record expects a record with no 'signature' key yet, not an already-signed one"
        raise ValueError(msg)

    from bernstein.core.security.agent_card_signer import _b64url, canonicalize_jcs

    canonical_bytes = canonicalize_jcs(record)
    raw_signature = _sign_raw_ed25519(canonical_bytes, private_key_pem)
    return {**record, "signature": _b64url(raw_signature)}


def verify_trust_record(record: dict[str, Any], public_key_pem: bytes) -> bool:
    """Verify a signed Trust Record dict's bare Ed25519 ``signature``.

    Reconstructs the exact pre-image :func:`sign_trust_record` signed --
    *record* with only the ``signature`` key removed, no other member added
    or substituted -- and checks it against *public_key_pem*. Works on any
    parsed TRACE v0.2 record, not only ones this module minted, which is
    what makes it useful for cross-checking third-party or reference-suite
    records against this producer's own verifier.

    Returns:
        ``True`` if the signature verifies, ``False`` if it does not.

    Raises:
        ValueError: *record* has no ``signature`` field, ``signature`` is
            not valid base64url, or *public_key_pem* is not an Ed25519 key.
            These are structurally malformed input, not a "verification
            failed" outcome a caller should treat the same way as a bad
            signature.
    """
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    from bernstein.core.security.agent_card_signer import _b64url_decode, canonicalize_jcs

    if "signature" not in record or not record["signature"]:
        msg = "verify_trust_record: record has no 'signature' field"
        raise ValueError(msg)

    raw_signature = _b64url_decode(record["signature"])
    body = {k: v for k, v in record.items() if k != "signature"}
    public_key = load_pem_public_key(public_key_pem)
    if not isinstance(public_key, Ed25519PublicKey):
        msg = f"verify_trust_record requires an Ed25519 public key, got {type(public_key).__name__}"
        raise ValueError(msg)
    try:
        public_key.verify(raw_signature, canonicalize_jcs(body))
    except InvalidSignature:
        return False
    return True


def spiffe_subject_for_execution(run_id: str, exec_id: str) -> str:
    """Return the execution-scoped SPIFFE subject for one execution hop.

    ``spiffe://bernstein.run/run/<run_id>/exec/<exec_id>``. Used by every
    Trust Record :meth:`TrustRecordEmitter.emit_trust_record` mints today.

    Raises:
        ValueError: *run_id* or *exec_id* is empty or contains ``/``
            (which would let a caller inject extra SPIFFE path segments).
    """
    _require_spiffe_segment(run_id, "run_id")
    _require_spiffe_segment(exec_id, "exec_id")
    return f"spiffe://{_SPIFFE_TRUST_DOMAIN}/run/{run_id}/exec/{exec_id}"


def spiffe_subject_for_aggregate(run_id: str) -> str:
    """Return the run-scoped (aggregate) SPIFFE subject for a whole run.

    ``spiffe://bernstein.run/run/<run_id>`` -- no ``/exec/<id>`` suffix.
    Used by :meth:`TrustRecordEmitter.emit_aggregate_trust_record`.

    Raises:
        ValueError: *run_id* is empty or contains ``/``.
    """
    _require_spiffe_segment(run_id, "run_id")
    return f"spiffe://{_SPIFFE_TRUST_DOMAIN}/run/{run_id}"


def _require_spiffe_segment(value: str, name: str) -> None:
    if not value or not _SPIFFE_SEGMENT_RE.match(value):
        msg = f"{name}={value!r} is not a valid SPIFFE path segment (must be non-empty and contain no '/')"
        raise ValueError(msg)


def _default_installed_digest() -> str:
    """Best-effort ``sha256:``-prefixed digest identifying the installed build.

    Not the original ``.whl`` artifact's own hash -- pip does not retain the
    wheel file after installation, so there is nothing on disk to re-hash.
    This instead hashes the sorted list of ``path:content-hash`` entries for
    every file the installed distribution recorded
    (``importlib.metadata``'s ``RECORD``-derived per-file SHA-256 digest,
    ``PackagePath.hash`` -- *not* merely the path strings themselves, which
    are identical for two installs with the same file layout but different
    file *contents* and so would not be a content digest at all). The result
    therefore changes if and only if the installed content does. A file with
    no recorded hash (``RECORD`` itself cannot hash its own still-being-
    written contents) still contributes its path with an empty hash slot,
    so its presence is not silently dropped from the digest. Falls back to
    the installed version string when the distribution's file list is empty
    or unavailable (e.g. editable/no-RECORD installs), and to an all-zero
    placeholder when ``bernstein`` itself is not resolvable as an installed
    distribution at all (should not happen outside of unusual embeddings).
    """
    import importlib.metadata as metadata

    try:
        dist = metadata.distribution("bernstein")
    except metadata.PackageNotFoundError:
        return f"sha256:{'0' * 64}"
    try:
        files = dist.files or ()
    except Exception:
        files = ()
    entries = sorted(
        f"{file_path}:{file_path.hash.value}" if file_path.hash is not None else f"{file_path}:" for file_path in files
    )
    digest_input = "\n".join(entries).encode("utf-8") if entries else dist.version.encode("utf-8")
    return f"sha256:{hashlib.sha256(digest_input).hexdigest()}"


class TrustRecordEmitter:
    """Emitter for TRACE 0.2 compliant Trust Records from journal data.

    The emitter reads a journal file, extracts the execution's model,
    policy, data classification, and tool-call surface, signs with the
    install identity, and returns canonical JSON.
    """

    def __init__(
        self,
        *,
        install_rev_getter: Callable[[], str] | None = None,
        get_private_key_pem: Callable[[], bytes] | None = None,
        get_installed_digest: Callable[[], str] | None = None,
    ) -> None:
        """Initialize emitter with optional injectable dependencies.

        Args:
            install_rev_getter: Callable returning the install revision
                token. Defaults to :func:`bernstein.core.identity.install_rev.get_install_rev`.
            get_private_key_pem: Callable returning the install Ed25519
                private key PEM. Defaults to loading from the install
                keystore via :func:`default_keystore`.
            get_installed_digest: Callable returning the
                ``build_provenance.digest`` value (``"sha256:..."``).
                Defaults to :func:`_default_installed_digest`. Fixture
                generators inject a deterministic stand-in here instead of
                the real installed-build digest, which is not reproducible
                across environments.
        """
        self._install_rev_getter = install_rev_getter
        self._private_key_provider = get_private_key_pem
        self._installed_digest_provider = get_installed_digest

    def _get_install_rev(self) -> str:
        """Return the install revision token."""
        if self._install_rev_getter is not None:
            return self._install_rev_getter()
        from bernstein.core.identity.install_rev import get_install_rev

        return get_install_rev()

    def _get_private_key_pem(self) -> bytes:
        """Return the install Ed25519 private key PEM."""
        if self._private_key_provider is not None:
            return self._private_key_provider()
        from bernstein.core.identity.http_signing import default_keystore

        private_pem, _ = default_keystore().load_or_generate()
        return private_pem

    def _get_installed_digest(self) -> str:
        """Return the ``build_provenance.digest`` value."""
        if self._installed_digest_provider is not None:
            return self._installed_digest_provider()
        return _default_installed_digest()

    def _build_unsigned_record(
        self,
        journal_path: Path,
        run_id: str,
        exec_id: str,
        *,
        kid: str,
        parent_record: str | None = None,
        credential_id: str | None = None,
    ) -> TrustRecord:
        """Build the unsigned Trust Record from journal data.

        Args:
            journal_path: Path to the journal.jsonl file.
            run_id: The overall (potentially multi-hop) run identifier --
                shared by every hop of one delegated run. Not read from
                *journal_path*; see the module docstring's "run_id/exec_id
                derivation" section for the full contract and how a
                verifier reproduces it.
            exec_id: This execution hop's identifier, scoped within
                *run_id*. Expected to equal the identifier that names
                *journal_path* on disk (``journal_path.parent.name`` for a
                journal built through :class:`~bernstein.core.replay.journal.EventJournal`)
                -- see the module docstring.
            kid: Key identifier for the install signing key, embedded as
                ``cnf.jwk.kid`` (and therefore covered by the signature,
                unlike the pre-#4760 shape which carried it in a
                signature-adjacent object outside the schema's field set).
            parent_record: The parent execution's own canonical signed
                record -- a prior return value of :meth:`emit_trust_record`
                -- when this call is one hop of a delegated multi-agent
                run. ``None`` (the default) for a root execution.
            credential_id: The delegation credential this hop acted under.
                Required exactly when *parent_record* is given, and must be
                non-empty (the schema requires
                ``delegation.credential_id`` to have ``minLength: 1``).

        Returns:
            TrustRecord with every field populated but no signature.

        Raises:
            ValueError: The journal's hash chain does not verify, the
                journal has no events (no completion time to source ``iat``
                from), the journal names no model or gate config,
                *credential_id* was given but is an empty string, *
                *parent_record* was given but is not valid JSON, or
                *parent_record* and *credential_id* disagree about whether
                this is a delegated hop.
        """
        if (parent_record is None) != (credential_id is None):
            msg = "parent_record and credential_id must be given together (child hop) or not at all (root hop)"
            raise ValueError(msg)
        if credential_id is not None and not credential_id:
            msg = (
                "credential_id must be non-empty when given "
                "(schema requires delegation.credential_id to have minLength 1)"
            )
            raise ValueError(msg)

        # Read journal file
        try:
            lines = journal_path.read_text(encoding="utf-8").strip().splitlines()
        except OSError:
            lines = []

        events = []
        for line in lines:
            if line.strip():
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

        # Verify the journal's hash chain before trusting anything it
        # recorded. A tampered journal (reordered or mutated events) must
        # not produce a record; the error names the divergent step so a
        # repairer can find it (R12: verifiers name the diverging element,
        # never a bare true/false).
        from bernstein.core.replay.journal import JournalVerifyResult, verify_events

        verdict: JournalVerifyResult = verify_events(events)
        if not verdict.chain_consistent:
            reason = verdict.errors[0] if verdict.errors else f"step {verdict.divergent_index}"
            raise ValueError(f"journal chain broken: {reason}")

        if not events:
            msg = f"journal {journal_path} has no events: no completion time to source iat/appraisal.timestamp from"
            raise ValueError(msg)

        iat = round(float(events[-1].get("ts", 0.0)))

        model = _extract_model(events, journal_path)
        gate_config = _extract_last(events, "gate_config")
        if gate_config is None:
            msg = f"journal {journal_path} names no gate_config: cannot compute policy.bundle_hash"
            raise ValueError(msg)
        data_class = _extract_last(events, "data_class", default=_DEFAULT_DATA_CLASS)

        subject = spiffe_subject_for_execution(run_id, exec_id)

        runtime: dict[str, str] = {
            "platform": "software-only",
            "measurement": _ALL_ZERO_SHA256_MEASUREMENT,
        }

        from bernstein.core.security.agent_card_signer import canonicalize_jcs

        bundle_hash = f"sha256:{hashlib.sha256(canonicalize_jcs(gate_config)).hexdigest()}"
        policy: dict[str, Any] = {"bundle_hash": bundle_hash, "enforcement_mode": _ENFORCEMENT_MODE}

        tool_transcript = _build_tool_transcript(events)

        build_provenance: dict[str, Any] = {
            "slsa_level": _SLSA_LEVEL,
            "digest": self._get_installed_digest(),
            "provenance_uri": _RELEASE_PAGE_URI,
        }

        appraisal: dict[str, Any] = {
            "status": "none",
            "verifier": _APPRAISAL_VERIFIER_URI,
            "timestamp": iat,
        }

        public_key_raw = _ed25519_public_key_raw(self._get_private_key_pem())
        from bernstein.core.security.agent_card_signer import _b64url

        cnf: dict[str, Any] = {
            "jwk": {
                "kty": "OKP",
                "crv": "Ed25519",
                "x": _b64url(public_key_raw),
                "kid": kid,
            }
        }

        delegation: dict[str, Any] | None = None
        if parent_record is not None:
            assert credential_id is not None  # enforced by the paired-args check above
            try:
                parent_doc = json.loads(parent_record)
            except json.JSONDecodeError as exc:
                msg = f"parent_record is not valid JSON: {exc}"
                raise ValueError(msg) from exc
            if not isinstance(parent_doc, dict):
                msg = f"parent_record must be a JSON object, got {type(parent_doc).__name__}"
                raise ValueError(msg)
            from bernstein.core.security.agent_card_signer import canonicalize_jcs

            parent_hash = hashlib.sha256(canonicalize_jcs(parent_doc)).hexdigest()
            delegation = {
                "parent_record_hash": f"sha256:{parent_hash}",
                "credential_id": credential_id,
            }

        references = _build_references(events) or None

        return TrustRecord(
            eat_profile=_EAT_PROFILE,
            iat=iat,
            subject=subject,
            model=model,
            runtime=runtime,
            policy=policy,
            data_class=data_class,
            tool_transcript=tool_transcript,
            build_provenance=build_provenance,
            appraisal=appraisal,
            cnf=cnf,
            delegation=delegation,
            references=references,
            signature="",
        )

    def _sign_record(self, record: TrustRecord) -> TrustRecord:
        """Sign a Trust Record using Ed25519.

        Per the schema, ``signature`` is a bare base64url Ed25519 signature
        over the JCS canonicalisation of the record's own present fields
        (see :func:`_record_dict_without_signature` /
        :func:`sign_trust_record`) -- no JOSE header, no detached-JWS
        framing. The key id lives in ``cnf.jwk.kid`` (already set on
        *record* by :meth:`_build_unsigned_record`), not alongside this
        signature.

        Args:
            record: Unsigned Trust Record.

        Returns:
            TrustRecord with signature populated.
        """
        body = _record_dict_without_signature(record)
        signed_dict = sign_trust_record(body, self._get_private_key_pem())

        from dataclasses import replace

        return replace(record, signature=signed_dict["signature"])

    def emit_trust_record(
        self,
        journal_path: Path,
        run_id: str,
        exec_id: str,
        *,
        parent_record: str | None = None,
        credential_id: str | None = None,
    ) -> str:
        """Emit a TRACE 0.2 Trust Record as canonical JSON.

        Args:
            journal_path: Path to the journal.jsonl file.
            run_id: The overall (potentially multi-hop) run identifier --
                shared by every hop of one delegated run. See
                :meth:`_build_unsigned_record` and the module docstring's
                "run_id/exec_id derivation" section for the full contract.
            exec_id: This execution hop's identifier, scoped within
                *run_id*. Expected to equal the identifier that names
                *journal_path* on disk -- see the module docstring.
            parent_record: The parent execution's own canonical signed
                record, when this call is one hop of a delegated
                multi-agent run (see :meth:`_build_unsigned_record`).
                ``None`` (the default) for a root execution.
            credential_id: The delegation credential this hop acted under.
                Required exactly when *parent_record* is given.

        Returns:
            Canonical JSON string of the signed Trust Record.
        """
        install_rev = self._get_install_rev()
        kid = f"install-{install_rev}"

        record = self._build_unsigned_record(
            journal_path,
            run_id,
            exec_id,
            kid=kid,
            parent_record=parent_record,
            credential_id=credential_id,
        )

        signed = self._sign_record(record)

        output = _record_dict_without_signature(signed)
        output["signature"] = signed.signature

        return json.dumps(output, sort_keys=True, separators=(",", ":"))

    def emit_aggregate_trust_record(
        self,
        run_id: str,
        member_records: Sequence[str],
    ) -> str:
        """Emit a run-level aggregate Trust Record over already-minted member records.

        Unlike :meth:`emit_trust_record`, this reads no journal: an
        aggregate did not itself execute anything, so every field it
        carries is a rollup over *member_records* rather than a fresh
        execution's own observations.

        Args:
            run_id: The run identifier every member in *member_records*
                shares (each member's own ``subject`` is
                ``spiffe://bernstein.run/run/<run_id>/exec/<exec_id>``).
            member_records: The exact canonical JSON strings
                :meth:`emit_trust_record` returned for each member
                execution, in the order they should appear in
                ``references``. Must be non-empty.

        Returns:
            Canonical JSON string of the signed aggregate Trust Record.

        Raises:
            ValueError: *member_records* is empty, a member is not valid
                JSON, or the members' ``data_class`` values disagree (roll
                up to the most restrictive).

        Rollup rules (documented here since there is no journal to point
        to for them):

        - ``iat``: the latest member ``iat`` -- the aggregate cannot be
          issued before every member it covers has completed.
        - ``model``: the last member's ``model``, mirroring the
          last-event-wins convention :meth:`_build_unsigned_record` already
          uses for a single execution's own model.
        - ``policy.bundle_hash``: ``sha256:`` + the hex SHA-256 of the JCS
          canonicalisation of the ordered list of member
          ``policy.bundle_hash`` values -- a hash *of* the constituent
          policy hashes, not a policy of its own. ``enforcement_mode`` is
          the fixed :data:`_ENFORCEMENT_MODE`, same as every execution
          record.
        - ``data_class``: the most restrictive value among members
          (``restricted < internal < confidential < public``). An aggregate
          covers all members, so its classification ceiling is the most
          restrictive member's ceiling.
        - ``tool_transcript``: ``call_count`` sums the members' counts;
          ``hash`` is ``sha256:`` + the hex SHA-256 of the JCS
          canonicalisation of the ordered list of member
          ``tool_transcript.hash`` values (a hash of hashes, same shape as
          ``policy.bundle_hash`` above).
        - ``build_provenance``/``appraisal``/``cnf``: built the same way as
          for an execution record (this producer's own build digest,
          self-declared "none" appraisal, this install's signing key).
        - ``delegation``: never present -- an aggregate is a rollup, not a
          hop acting under delegated authority.
        - ``references``: one ``{"rel": "member-execution", "id",
          "resolver", "digest"}`` entry per member, in the given order.
          ``id`` is the member's own ``subject``, which is what names it
          inside the resolver; ``digest`` is ``sha256:`` + the hex SHA-256 of
          the member's complete signed record canonicalised with JCS (RFC 8785)
          (the same convention ``delegation.parent_record_hash`` uses for a parent record),
          so a verifier resolves a member by name and binds it by recomputing
          that digest over the record it holds.
        """
        if not member_records:
            msg = "emit_aggregate_trust_record requires at least one member record"
            raise ValueError(msg)

        members: list[dict[str, Any]] = []
        for raw in member_records:
            try:
                members.append(json.loads(raw))
            except json.JSONDecodeError as exc:
                msg = f"a member record is not valid JSON: {exc}"
                raise ValueError(msg) from exc

        install_rev = self._get_install_rev()
        kid = f"install-{install_rev}"

        subject = spiffe_subject_for_aggregate(run_id)
        iat = max(int(member["iat"]) for member in members)
        model = dict(members[-1]["model"])

        runtime: dict[str, str] = {
            "platform": "software-only",
            "measurement": _ALL_ZERO_SHA256_MEASUREMENT,
        }

        from bernstein.core.security.agent_card_signer import canonicalize_jcs

        member_bundle_hashes = [member["policy"]["bundle_hash"] for member in members]
        policy_digest = hashlib.sha256(canonicalize_jcs(member_bundle_hashes)).hexdigest()
        policy: dict[str, Any] = {"bundle_hash": f"sha256:{policy_digest}", "enforcement_mode": _ENFORCEMENT_MODE}

        data_classes = {member["data_class"] for member in members}
        # Members disagree on data_class (e.g., a narrowed child
        # hop "restricted" aggregated alongside parent's "internal").
        # Roll up to the most restrictive value (restricted < internal < confidential < public).
        _DATA_CLASS_PRECEDENCE = {"restricted": 0, "internal": 1, "confidential": 2, "public": 3}
        data_class = min(
            data_classes,
            key=lambda dc: _DATA_CLASS_PRECEDENCE.get(dc, 99),
        )

        call_count = sum(int(member["tool_transcript"]["call_count"]) for member in members)
        member_transcript_hashes = [member["tool_transcript"]["hash"] for member in members]
        transcript_digest = hashlib.sha256(canonicalize_jcs(member_transcript_hashes)).hexdigest()
        tool_transcript: dict[str, Any] = {"hash": f"sha256:{transcript_digest}", "call_count": call_count}

        build_provenance: dict[str, Any] = {
            "slsa_level": _SLSA_LEVEL,
            "digest": self._get_installed_digest(),
            "provenance_uri": _RELEASE_PAGE_URI,
        }

        appraisal: dict[str, Any] = {
            "status": "none",
            "verifier": _APPRAISAL_VERIFIER_URI,
            "timestamp": iat,
        }

        public_key_raw = _ed25519_public_key_raw(self._get_private_key_pem())
        from bernstein.core.security.agent_card_signer import _b64url

        cnf: dict[str, Any] = {
            "jwk": {
                "kty": "OKP",
                "crv": "Ed25519",
                "x": _b64url(public_key_raw),
                "kid": kid,
            }
        }

        # The member digest belongs in ``digest``, not in ``id``. §3.1.2 gives
        # the two fields different jobs -- ``id`` identifies the referenced
        # fact within the resolver's system, ``digest`` binds it to specific
        # bytes -- and a verifier that content-binds references reads
        # ``digest``. Carrying the digest as an ``id`` and omitting ``digest``
        # validates (``digest`` is optional and ``rel`` is open), so nothing
        # catches it; the entries are simply not content-bound in the
        # vocabulary the block defines, and the set property is readable only
        # by whoever produced both sides. The sibling ``produced-artifact``
        # entry on an execution record already splits the two correctly.
        references = [
            {
                "rel": "member-execution",
                "id": member["subject"],
                "resolver": _MEMBER_EXECUTION_RESOLVER_URI,
                "digest": f"sha256:{hashlib.sha256(canonicalize_jcs(member)).hexdigest()}",
            }
            for member in members
        ]

        record = TrustRecord(
            eat_profile=_EAT_PROFILE,
            iat=iat,
            subject=subject,
            model=model,
            runtime=runtime,
            policy=policy,
            data_class=data_class,
            tool_transcript=tool_transcript,
            build_provenance=build_provenance,
            appraisal=appraisal,
            cnf=cnf,
            delegation=None,
            references=references,
            signature="",
        )

        signed = self._sign_record(record)

        output = _record_dict_without_signature(signed)
        output["signature"] = signed.signature

        return json.dumps(output, sort_keys=True, separators=(",", ":"))


def _extract_last(events: list[dict[str, Any]], key: str, *, default: Any = None) -> Any:
    """Return the value of *key* on the last event that carries it, or *default*."""
    for event in reversed(events):
        if key in event:
            return event[key]
    return default


def _extract_model(events: list[dict[str, Any]], journal_path: Path) -> dict[str, Any]:
    """Return the ``model`` member sourced from the last ``model_id``-bearing event."""
    provider = _extract_last(events, "model_provider")
    model_id = _extract_last(events, "model_id")
    if model_id is None or provider is None:
        msg = f"journal {journal_path} names no model_provider/model_id: cannot populate required 'model' member"
        raise ValueError(msg)
    model: dict[str, Any] = {"provider": provider, "model_id": model_id}
    version = _extract_last(events, "model_version")
    if version is not None:
        model["version"] = version
    return model


#: Journal bookkeeping fields never treated as tool-call payload content.
_JOURNAL_CHAIN_FIELDS = frozenset({"index", "event", "prev_hash", "payload_hash", "event_hash", "ts", "elapsed_s"})


def _build_tool_transcript(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold every ``tool_call`` event, in journal order, into ``tool_transcript``.

    Always returns a populated object, even at zero calls: "no tool calls
    happened" is a fact this execution attests to, not an unknown value to
    omit the way a missing timestamp is.
    """
    from bernstein.core.security.agent_card_signer import canonicalize_jcs

    calls = [
        {k: v for k, v in event.items() if k not in _JOURNAL_CHAIN_FIELDS}
        for event in events
        if event.get("event") == "tool_call"
    ]
    digest = hashlib.sha256(canonicalize_jcs(calls)).hexdigest()
    return {"hash": f"sha256:{digest}", "call_count": len(calls)}


def _build_references(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return one ``rel: "produced-artifact"`` entry per ``artifact_produced`` event.

    No ``rel: "evidence"`` entry is ever produced for an execution record:
    ``tool_transcript`` covers the journal now. The ``member-execution``
    relation belongs to the run-level aggregate record
    (:meth:`TrustRecordEmitter.emit_aggregate_trust_record`), never to one
    built from a journal.
    """
    references: list[dict[str, Any]] = []
    for event in events:
        if event.get("event") != "artifact_produced":
            continue
        references.append(
            {
                "rel": "produced-artifact",
                "id": str(event["artifact_id"]),
                "resolver": str(event["resolver"]),
                "digest": str(event["digest"]),
            }
        )
    return references


def _ed25519_public_key_raw(private_key_pem: bytes) -> bytes:
    """Return the raw 32-byte Ed25519 public key for a PKCS8 private key PEM."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private_key = serialization.load_pem_private_key(private_key_pem, password=None)
    if not isinstance(private_key, Ed25519PrivateKey):
        msg = "install identity key is not Ed25519"
        raise ValueError(msg)
    return private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )


def _sign_raw_ed25519(canonical_bytes: bytes, private_key_pem: bytes) -> bytes:
    """Return the raw Ed25519 signature over *canonical_bytes*.

    No JOSE header, no detached-JWS framing: the schema's ``signature``
    member is "a signature ... over the canonical JSON form of the record",
    full stop -- this is that signature, and nothing else.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private_key = serialization.load_pem_private_key(private_key_pem, password=None)
    if not isinstance(private_key, Ed25519PrivateKey):
        msg = "_sign_raw_ed25519 requires an Ed25519 (EdDSA) private key"
        raise ValueError(msg)
    return private_key.sign(canonical_bytes)
