# Hardware evidence and the receipt substrate

`core/identity/hardware_attestation.py` owns offline PIV chain/extension checks,
FIDO2 packed registration and assertion checks, immutable `HardwarePolicy`, and
serial-free deterministic `EvidenceVerdict` JSON. Packaged `hardware_roots/`
contains public vendor PEM trust anchors and their source/fingerprint manifest.

The module registers `key-attestation/piv` and `approval/fido2-assertion` through
`register_receipt_kind`; the common kind loader imports it. A narrow optional
context callback receives the actual envelope plus externally configured policy.
This prevents a PIV payload from self-asserting a signer public key that differs
from the verified outer signer. Context validation also returns advisory warnings.
Existing payload-only kind registration remains unchanged.

The common `sign_receipt` and `verify_receipt` functions still own envelope,
digest, canonical bytes and detached signatures. Their shared signing primitives
opt into P-256 as well as Ed25519; unrelated catalog callers retain the default
Ed25519 restriction. There is no second receipt canonicalizer or signing path.

Evidence verification is a pure function of supplied bytes and immutable policy
(including explicit time or a signed historical timestamp and a counter snapshot).
The verifier does not learn trust from payload claims, operate hardware, persist
counters, call a metadata service, upgrade TRACE trust, or approve an action by
itself. See [operator contract](../operations/hardware-key-evidence.md) for wire
fields, assumptions and unsupported WebAuthn features.
