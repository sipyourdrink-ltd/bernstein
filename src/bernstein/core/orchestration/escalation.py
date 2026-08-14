def assemble_escalation_receipt(self, run_id, run_journal):
    if not run_journal:
        raise EscalationError(f"no journal for run {run_id!r}")
    # ... rest of the method remains the same ...
