## Auditor conformance suite reaches all 21 registered questions

Completed the live auditor conformance suite under
`tests/integration/conformance/auditor/` by adding the remaining authority
(#5059) and data/endpoint (#5061) vectors against the per-session exported
bundle (#5054):

- `tests/integration/conformance/auditor/test_authority_vectors.py` registers
  questions 3, 4, 5, and 21 as strict expected failures documenting the missing
  delegation grant, permitted-action scope, boundary-compliance check, and
  co-grantee propagation fields.
- `tests/integration/conformance/auditor/test_data_endpoint_vectors.py`
  registers question 9 as passing (verifying `model.request`, `model_call`
  including `input_sha256`, and `agent_spawned` endpoint identity) and
  questions 8 and 10 as strict expected failures for missing exported
  sensitivity classification and endpoint admission decisions.
- `scripts/auditor_scoreboard.py` now reports `asked 21/21, answered 4/21`.
