## Accurate PluginTrust claims for signature and metadata presence

`PluginTrust` docstrings and CLI warning panel labels claimed "valid
cryptographic signature" and "verified provenance" for checks that only
inspect the presence of a `.signature` file or minimal metadata in
`pyproject.toml`. The docstrings and CLI warning signals now accurately
describe what is inspected ("Signature file present", "Metadata present")
without claiming unperformed cryptographic signature verification or provenance
attestation. Risk level derivation and trust score computations remain
unchanged (#5676).
