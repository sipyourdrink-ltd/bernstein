## Failed rollback attempts retain a receipt

Rollback failures now persist a checksum-protected receipt naming the proposal,
the error, and exactly which files were restored before failure. Missing or
invalid backup records and bookkeeping failures raise `RollbackError` while
retaining the backup manifest. If receipt storage also fails, the error reports
both failures. Existing success receipts keep their format (#5408).
