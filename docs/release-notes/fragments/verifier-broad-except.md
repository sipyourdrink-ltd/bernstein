## A DSSE verification failure names its cause

`_verify_intoto` walked a receipt's DSSE signatures inside `except (InvalidSignature, Exception)`. The tuple reads as though only a bad signature is caught, but `Exception` subsumes `InvalidSignature`, so the clause swallowed everything: a signature entry that was not an object, a `sig` field that was not a string or not base64, and any fault raised from inside the verification itself. All of them arrived at the same message, `DSSE signature does not verify against the supplied public key`, which pointed the reader at their key.

The loop now rejects each entry for a stated reason and reports those reasons in the error, and only `InvalidSignature` counts as a signature that did not verify. An unexpected exception propagates instead of being recorded as a failed verification, so a bug in the attestation path can no longer present itself as an authenticity failure.

The same tuple shape appeared once more, in the volunteer runner's best-effort comment fetch. That one is genuinely best-effort, so it keeps its broad catch, written as a single honest `except Exception:` with the `intentional-broad-except` marker the project's doctrine asks for.
