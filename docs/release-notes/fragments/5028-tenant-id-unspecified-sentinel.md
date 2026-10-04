# Security / Identity

- `tenant_id`: removed silent `"default"` string default on `AgentCredential`, `TokenUsage`, and `_ChatTaskRequest`. Records created without an explicit tenant now carry `UNSPECIFIED_TENANT` (`<unspecified>`), preventing records with missing tenants from collapsing into the `"default"` tenant and ensuring isolation checks fail closed (#5028).
