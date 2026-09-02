## Apply receipts on the target and a freshness check for missing-or-stale

The receipt-on-target pattern from skill installs is generalised to all entity
kinds. A target now stores a `TargetReceipt` anchored in the govern lineage run,
and `check_receipt_current` emits the `receipt_not_current` finding both when no
receipt exists and when the receipt has aged past the configured window.
Issue #5087.
