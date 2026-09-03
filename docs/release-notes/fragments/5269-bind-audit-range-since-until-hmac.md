## Run receipts bind the declared audit window, not just its content hash

`since`, `until`, and `head_hmac` on an opt-in `audit_range` block described which window
the embedded audit events came from, but only the recomputed content head was part of the
signed subject. A receipt could be edited after signing to relabel that window (or swap
`head_hmac`) with no effect on verification. All three are now bound into the signature the
same way the content head already was, so relabelling any of them fails closed as tampered
(#5269).
