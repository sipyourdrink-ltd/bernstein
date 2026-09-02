### Fixed

- A review-mode approval gate that timed out with no decision resolved to
  **approved** unless the caller had passed an explicit timeout, so an operator
  who configured no timeout got the default wait followed by an auto-approval —
  an approval record indistinguishable from one a person granted. The timeout
  now fails closed in every path: `_default_poll_decision` reports an expiry as
  `"timed_out"` rather than inventing an outcome, and `ApprovalGate` rejects on
  expiry. `ApprovalResult` carries `resolution` (`"decided"` / `"timed_out"`)
  so a reader of the trail can tell an expiry from a real decision, and the
  "do not block" behaviour is still available through a named
  `approve_on_timeout` opt-in that still records the expiry as such.
  ([#5051](https://github.com/sipyourdrink-ltd/bernstein/issues/5051))
