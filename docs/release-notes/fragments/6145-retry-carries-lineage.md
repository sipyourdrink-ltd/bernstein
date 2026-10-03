## Tick-loop task retries now carry full lineage

Retried tasks created by the tick loop (`maybe_retry_task`) now keep
`completion_signals`, `owned_files` and `depends_on` in the retry request,
matching what the reap path (`retry_or_fail_task`) already carried. Previously
the tick-loop path dropped all three silently. (#6145)
