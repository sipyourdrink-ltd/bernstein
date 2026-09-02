## Evidence envelope v1 schema

Bernstein now ships a versioned evidence envelope format. An evidence envelope is
a portable, signed artefact that ties an installation identity to a bounded set
of governance decisions and the recorded evidence behind each one. It carries
six sections: ``principal`` (the acting identity and its key), ``grants`` (the
authority chain), ``decisions`` (one record per authorisation), ``evidence``
(the digests anchoring each decision to the run record), ``coverage`` (what the
envelope does *not* account for — required, never optional), and a detached
JWS signature over the five data sections.

Canonical form is JCS (RFC 8785); the signature is a detached JWS in compact
form. The schema is committed under ``schemas/evidence-envelope-v1.json`` and
a golden vector lives under ``tests/fixtures/evidence-envelope-vectors/``.
The Python module ``bernstein.core.security.evidence_envelope`` exports the
pinned identifiers and canonical-form helpers; it is the format surface only —
it does not build or verify envelopes.

(#5063)
