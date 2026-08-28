"""Volunteer protocol — shared document substrate.

This sub-package provides the cryptographic primitives for signing and
verifying volunteer protocol documents.  Every future document type
(candidacy, task-finish, dispute, etc.) reuses the helpers here rather than
each re-implementing the three-step sign/verify dance.

Submodules:

* :mod:`.documents` — canonical-bytes, hash, sign, and verify helpers built
  on top of :mod:`bernstein.core.security.audit_dsse`.
"""
