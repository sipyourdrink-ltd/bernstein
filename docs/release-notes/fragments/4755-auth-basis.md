## Adapters declare the authentication mechanism they use

Every shipped adapter contract now carries an explicit `auth_basis`, surfaced on `ContractSpec` and `AdapterCapabilityProfile`, so the mechanism an adapter authenticates with is readable from the contract rather than inferred from its configuration (#3875).
