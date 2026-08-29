## 3765 - Route onboarded adapter evidence through admission chain and remove generic from exemption list

This release ensures that evidence gathered during the onboarding process is properly routed through the receipt-gated admission chain, making an onboarded adapter's receipt indistinguishable from a hand-written adapter's. Previously, `generic` adapters were exempt from admission entirely, meaning they could not prove their authority through a sealed receipt.

Key changes:
- Removed `generic` from `ADMISSION_EXEMPT` in admission.py so onboarded generic adapters must go through the admission gate
- Updated adapters_verify_cmd.py to route `generic` through the admission gate rather than exempting it
- Added comprehensive tests in test_adapter_onboard_admit.py to verify the new flow produces sealed receipts

This change affects:
- Onboarded adapters (evidence now verified through existing chain)
- The generic adapter path (now requires a contract and sealed receipt)
- Release notes documentation

No user-facing functional changes except that previously exempt generic adapters now require admission evidence to be verified, which is the correct behavior for production adapters.