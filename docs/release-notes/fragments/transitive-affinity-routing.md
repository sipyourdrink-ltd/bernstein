## Preserve routing hints across transitive file affinity

Tasks connected through overlapping files stay in separate batches when their
explicit model, CLI adapter, or preferred-agent hints conflict. Previously, an
unpinned task touching two groups' files could join those groups into one batch
even when they required different agents. Compatible groups still combine, and
every task remains scheduled exactly once.
