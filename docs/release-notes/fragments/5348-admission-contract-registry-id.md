## Admission preflight resolves adapter contract by registry ID

When spawning with a model whose provider prefix is not in the adapter registry (such as a
custom gateway proxy), admission preflight and contract verification now resolve against the
canonical adapter registry ID rather than the human display name, preventing erroneous
`no_contract` refusal receipts (#5348).
