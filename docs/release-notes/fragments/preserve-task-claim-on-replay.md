## Preserve task claims when replaying the journal

Task journal replay restores the claim timestamp and claimant session for pending cooperative stops. When an older entry omits either field, replay keeps its previous value. An explicit null clears that field.
