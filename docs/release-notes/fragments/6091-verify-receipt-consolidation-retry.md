## Receipt protocol consolidation

The guard test added in #6091 asserted exactly one definition each for
`verify_receipt`, `sign_receipt`, and `canonical_receipt_bytes` under
`src/bernstein`. Seven modules defined their own versions with different
signatures and behaviours. Those modules now use distinct names for their
receipt-type-specific functions, leaving `bernstein.core.receipts.protocol` as
the single source of the three protocol primitives.
