## Outbound boundary model-call check records (`call_llm` boundary coverage)

Every outbound model call across the orchestrator boundary now emits
an immutable, content-addressed check record (`OutboundCheckRecord`).
Unchecked calls classify as `UNVERIFIED` under the absence coverage
taxonomy, ensuring that "checked and clean" and "never checked" are
provably distinct states. The record commits to the request strictly
by cryptographic digest, never serialising prompt text or secrets into
the lineage chain (#5477).
