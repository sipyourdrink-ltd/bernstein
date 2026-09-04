FIXED: 2 of 2 blocking findings

F1 (Duplicate `verify_receipt` breaks the one-verifier guard): Fixed by pass 1 commit fd479501 — removed the duplicate `def verify_receipt`, `FieldError`, `ReceiptVerification`, and `_parse_field_errors` from `src/bernstein/core/security/change_receipt.py`. The test already uses `change_receipt_payload_errors` directly (no `verify_receipt` import). Guard test now passes.

F2 (`ReceiptVerification` collides with the protocol's same-named class): Disappeared with F1 — removed by the same pass 1 commit.
