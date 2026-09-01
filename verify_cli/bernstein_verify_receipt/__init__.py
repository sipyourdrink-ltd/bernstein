"""bernstein-verify-receipt - standalone auditor CLI for Bernstein audit receipts.

This package MUST NOT import from `bernstein.*` at runtime. The whole point
of shipping it as a separate wheel is that an auditor on an air-gapped
laptop with only `cryptography` + `cbor2` + `click` can verify an audit
receipt without installing the full orchestration stack.

What it checks (per format, plus the shared subject binding):

* ``cose`` - COSE_Sign1 (RFC 9052) EdDSA signature over the subject
  digest, and payload == recomputed chain head.
* ``intoto`` - DSSE / in-toto v1 Statement signature, and the statement
  subject digest == recomputed chain head.
* ``transparency`` - RFC 6962 style signed tree head signature, Merkle root
  rebuilt from the embedded range, inclusion proof of the chain-head leaf,
  and subject binding.

Exit codes: ``0`` all enabled checks passed, ``1`` a check failed,
``2`` bad arguments or unreadable inputs.
"""

from __future__ import annotations

__version__ = "1.0.0"
