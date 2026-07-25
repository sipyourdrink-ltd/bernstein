"""Audit chain helpers for cross-subsystem event recording.

This module exposes :class:`AuditChainStore`, a thin facade over
:class:`bernstein.core.security.audit.AuditLog` that surfaces the
``prev_chain_digest`` (the HMAC of the most recent event) to callers
that need to embed it inside an event payload (for example
``multimodal.attach``).

The module also defines additive event-type constants used by
subsystems that emit structured records into the HMAC-chained log.
New event types should be added below as ``EVENT_<UPPER_SNAKE>``
string constants -- never edit existing entries.

Concurrent-edit policy
----------------------
Sibling agents may extend this module with additional event-type
constants and helper functions; the ``AuditChainStore`` class itself
is treated as the stable surface. Helpers MUST:

* Accept the chain instance, not import it as a singleton.
* Call ``chain.log_with_prev_digest`` so that ``prev_chain_digest``
  is captured in ``details`` before the HMAC is computed.
* Never mutate existing event-type constants.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

from bernstein.core.security.audit import (
    AGENT_FRESH_RESTART_ON_RETRY as AGENT_FRESH_RESTART_ON_RETRY,
)
from bernstein.core.security.audit import (
    AuditEvent,
    AuditLog,
    ChainScanCursor,
    ChainScanResult,
)

# ---------------------------------------------------------------------------
# Additive event-type constants
# ---------------------------------------------------------------------------
# IMPORTANT: never modify or remove existing constants below. Add new
# constants only. Sibling agents may concurrently append to this list.

#: Issue #1797 -- emitted whenever an operator attaches an image to a
#: worker via ``bernstein run --attach`` (or the matching task YAML
#: ``attachments`` field). The event records the bytes' SHA-256, MIME
#: type, the requesting worker, the turn sequence number, the worktree
#: id, the operator install identity signature, and the previous chain
#: digest.
EVENT_MULTIMODAL_ATTACH = "multimodal.attach"

#: Issue #2242 -- emitted whenever the compaction sensitive-content gate
#: redacts a credential-shaped span, refuses a compaction outright, or
#: suppresses a rule via an operator allowlist entry. The event records
#: the task id, the rule id, the action taken, and the SHA-256 of the
#: offending span -- never the span content itself.
EVENT_COMPACTION_SENSITIVE_GATE = "compaction.sensitive_gate"

#: Issue #2246 -- emitted once per context compaction (proactive or
#: reactive). The event carries the full compaction receipt: pre/post
#: context SHA-256, token counts, validator verdicts, retry count, and
#: gate-outcome references. See
#: :mod:`bernstein.core.tokens.compaction_receipt` for the payload
#: builder and the verification helper that fails a run when a
#: journaled compaction lacks a chain-verifiable receipt.
EVENT_COMPACTION_RECEIPT = "compaction.receipt"

#: Issue #2245 -- emitted whenever ``bernstein cost profile-report``
#: writes a content-addressed per-profile cost report. The event
#: records the report's SHA-256, the ledger line-hash range the report
#: was computed from, and the previous chain digest, so a third party
#: holding the ledger can recompute the report byte-identically and
#: check it against the chain.
EVENT_COST_PROFILE_REPORT = "cost.profile_report"

#: Issue #2247 -- emitted whenever ``bernstein eval ab`` writes a
#: content-addressed profile comparison artifact. The event records the
#: artifact's SHA-256 plus the suite and profile-addendum hashes that
#: pin exactly what was compared, and the previous chain digest, so a
#: verifier holding the suite and the spend ledger can recompute the
#: artifact byte-identically and check it against the chain.
EVENT_EVAL_AB_COMPARISON = "eval.ab_comparison"

#: Issue #2606 -- emitted for every action a third-party autonomous browser /
#: computer-use agent decides on against a live UI. The event is the per-action
#: replay manifest: it carries the action anchor
#: (``sha256(prev_anchor, observation_hash, action)``), the prior anchor, the
#: pre-action screenshot's CAS SHA-256, the normalised DOM/accessibility digest,
#: the observation hash, the canonicalised action (kind / target / value
#: digest), the signed lineage entry hash, the worker identity, the worktree
#: id, and the previous chain digest. Mirrors ``multimodal.attach`` but for the
#: outbound action stream; a replay walks these events, re-hashes the stored
#: bytes, recomputes each anchor, and compares against the signed lineage head.
EVENT_COMPUTER_USE_ACTION = "computer_use.action"

#: Issue #2249 -- emitted once per applied role-template compression
#: (``bernstein templates compress``). The event carries the full
#: compression receipt: role, pre/post role-template directory digests,
#: token estimates, validator verdicts, adapter and model, per-file
#: content hashes, and the previous chain digest. See
#: :mod:`bernstein.core.tokens.template_compression` for the payload
#: builder and the verification helper.
EVENT_TEMPLATE_COMPRESSION_RECEIPT = "template.compression.receipt"

#: Issue #2249 -- emitted when ``bernstein templates restore`` reverses
#: a receipted compression byte-identically. The event references the
#: compression's correlation id and the verified pre/post role-template
#: directory digests.
EVENT_TEMPLATE_COMPRESSION_RESTORE = "template.compression.restore"

#: Issue #2298 -- emitted whenever a cross-session memory write (or
#: forget tombstone) is appended to the tamper-evident memory chain. The
#: event records the memory-chain entry hash, the lineage-spine
#: ``source_hash`` the record anchors to, the identity scope and
#: namespace, the actor, the originating run and step, and the entry
#: kind (``write`` or ``tombstone``) -- never the remembered claim
#: content. See :mod:`bernstein.core.memory.chain`.
EVENT_MEMORY_WRITE = "memory.write"

#: Issue #2301 -- emitted once per skill install. The event carries the
#: skill install receipt: the installed content hash, the authorising
#: manifest hash, the install id, and the spine anchor (the entry hash of
#: the receipt row in the install lineage spine). A verifier holding the
#: spine can recompute the anchor byte-identically and confirm the install
#: is chain-attested rather than registry-declared.
EVENT_SKILL_INSTALL_RECEIPT = "skill.install_receipt"

#: Issue #2301 -- emitted whenever a skill participates in a run. The event
#: binds the skill's content hash to the run journal head (the run's spine
#: head hash) so a later provenance query can recompute usage from verified
#: journal heads rather than from a mutable counter.
EVENT_SKILL_USAGE = "skill.usage"

#: Issue #2527 -- emitted whenever an install, doctor check, or spawn-side
#: injection refuses a skill because a transparency (inclusion / consistency)
#: proof failed or a signed revocation covers it. The chain-anchored refusal is
#: independently verifiable: an operator can prove a known-bad version was
#: contained, when, and why. See
#: :mod:`bernstein.core.skills.catalog.transparency` and
#: :mod:`bernstein.core.skills.catalog.revocation`.
EVENT_SKILL_VERIFICATION_REFUSAL = "skill.verification_refusal"

#: Issue #2306 -- emitted whenever a payment is authorized under a signed
#: spending mandate. The event carries the consent receipt binding
#: ``{mandate_hash, authorized_tool_calls_hash, settlement_ref,
#: journal_entry_hash}`` -- the journal entry hash anchors the receipt in the
#: mandate lineage spine so a verifier can recompute "this payment was
#: authorized by this exact intent" offline. Only hashes and the public
#: settlement reference are recorded -- never a payment credential.
EVENT_MANDATE_CONSENT_RECEIPT = "mandate.consent_receipt"

#: Issue #2306 -- emitted whenever a spending mandate is revoked. The event
#: records the revoked mandate hash and reason so an auditor can prove, from
#: the chain alone, that authority was withdrawn at a time; subsequent
#: actions under the mandate are refused.
EVENT_MANDATE_REVOCATION = "mandate.revocation"

#: Issue #2297 -- emitted when an operator resolves an approval over the
#: live event stream. The event anchors the decision to the exact run
#: journal entry the stream projected at decision time (the journal index
#: and its Merkle ``event_hash``), so a verifier can prove the approval was
#: made against the executed thread rather than a divergent view. The event
#: records the run id, the journal index, the entry hash, the decision, the
#: operator install signature, and the worktree id -- never diff content.
EVENT_THREAD_APPROVAL = "thread.approval"

#: Issue #2300 -- emitted whenever a signed OTel GenAI span set is projected
#: from a run's event journal. The event records the run id, the journal head
#: the projection anchors to, the derived OTLP trace id, the span count, and
#: the sha256 of the canonical signed span set. A verifier holding the journal
#: can reproject byte-identically and confirm the exported spans are a faithful
#: projection of the chain rather than free-standing telemetry -- never the
#: span attribute payloads themselves.
EVENT_OTEL_PROJECTION = "otel.projection"

#: Issue #2295 -- emitted once per ``bernstein fork --from-step``. The event
#: pins the fork lineage into the HMAC chain: the parent run id, the fork
#: step index, the content-addressed snapshot commit sha resumed at that
#: step, and the new child run id. Because the snapshot sha is the git
#: commit the child worktree was checked out from, a verifier holding the
#: parent journal and the snapshot ref can confirm the fork branched from
#: exactly the recorded step (a tampered ref no longer matches the sha).
EVENT_FORK_SNAPSHOT = "replay.fork_snapshot"

#: Issue #2294 -- emitted whenever a maker-checker or judge-panel gate produces
#: a signed adjudication record. The event mirrors the record's hashes and its
#: journal anchor into the chain so an operator can confirm, from the chain
#: alone, that a gate verdict bound the claimed inputs, rubric, and panel to a
#: named journal entry. Only hashes and the anchor are recorded -- never the raw
#: diff or rubric content.
EVENT_GATE_ADJUDICATION = "gate.adjudication"

#: Issue #2296 -- emitted whenever a signed pull-request review receipt is
#: anchored in the review lineage spine. The event mirrors the receipt's four
#: bound hashes (``issue_hash``, ``plan_hash``, ``journal_head``, ``diff_hash``),
#: the verdict, and the spine ``journal_entry_hash`` so an operator can prove,
#: from the audit chain alone, that a review receipt was emitted for a PR
#: linking issue to diff -- never the diff or issue body itself.
EVENT_REVIEW_RECEIPT = "review.receipt"

#: Issue #2307 -- emitted for every stateless MCP call. The stateless spec
#: revision removes the ``initialize`` handshake and ``Mcp-Session-Id``, so any
#: request can land on any server instance and the protocol no longer provides
#: cross-call ordering. This event anchors the call's continuity in the audit
#: chain instead of a session store: it records the run id, the MCP method, the
#: ordered call index, the content-derived W3C trace/span ids, the run journal
#: head the call was recorded against, and -- on a cache hit -- the content hash
#: of the producing run. A verifier can recompute the ordering from verified
#: chain entries rather than trusting a session id.
EVENT_MCP_STATELESS_CALL = "mcp.stateless_call"

#: Issue #2308 -- emitted whenever a deterministic outer-plan node delegates
#: mechanical execution to a native subagent (Claude Code, Codex, ...). The
#: event binds the plan-node hash (a pure function of the outer plan, so it is
#: identical across replays) to the native result's content hash and the run
#: journal entry the delegation was anchored at. A verifier can prove, from the
#: chain alone, that the cross-worker DAG crossed a delegation boundary at a
#: named node and anchored a specific (stochastic) result, without the record
#: exposing the native result payload itself.
EVENT_SUBAGENT_DELEGATION = "subagent.delegation"

#: Issue #2302 -- emitted once per recurring-goal schedule fire projection.
#: A recurring goal fire is a pure projection of ``(schedule_id, fire_time,
#: last_state)`` onto a canonical task graph; this event anchors the fire in
#: the HMAC chain by recording ``{schedule_id, fire_time, last_state_hash,
#: graph_hash}`` plus the lineage-spine ``journal_entry_hash`` the projection
#: was sealed into and the ``trigger_input_hash`` for a webhook / file-change
#: trigger (empty for a plain cron / RRULE fire). A verifier holding the
#: schedule and the fire time can re-run the projection and confirm the
#: recorded ``graph_hash`` byte-identically -- the fire is a hash, not a
#: trigger.
EVENT_SCHEDULE_FIRE_PROJECTION = "schedule.fire_projection"

#: Issue #2299 -- emitted when a stalled worker produces a signed, journal-
#: anchored escalation receipt. The receipt fixes the exact failure window by
#: binding the last N journal entries by their Merkle hash; this event mirrors
#: the receipt's identity into the HMAC-chained audit log so an operator can
#: prove, from the chain alone, that an escalation was emitted for a run and
#: worker with a given recommended action and resume fork point. Only
#: identifiers, the journal head at stall, the window size, the recommended
#: action, and the resume snapshot sha are recorded -- never journal payloads.
EVENT_ESCALATION_RECEIPT = "escalation.receipt"

#: Issue #2310 -- emitted whenever a webhook-node receipt is anchored in the
#: webhook-node lineage spine. Inbound receipts bind ``{event_hash,
#: journal_root}`` for a signed inbound event that spawned a run; outbound
#: receipts bind ``{result_hash, journal_head}`` for the signed result the
#: node returned to the calling bus. Mirroring the receipt into the chain lets
#: an operator prove, from the chain alone, that an otherwise-opaque no-code
#: flow step ran under a signed inbound event and produced a signed outbound
#: result. Only hashes and the source label are recorded -- never the webhook
#: body.
EVENT_WEBHOOK_NODE_RECEIPT = "webhook_node.receipt"

#: Issue #2309 -- emitted whenever a governance projection (RBAC access check or
#: per-subject budget check) produces a signed, anchored decision. The event
#: mirrors the decision's ``{subject, action, verdict, inputs_hash,
#: journal_entry_hash}`` into the chain so an operator can confirm, from the
#: chain alone, that a governance decision bound the claimed inputs to a named
#: spine entry. A denied access writes a ``deny`` verdict and a budget breach a
#: ``refuse`` verdict -- both are signed records, not merely logged. Only hashes,
#: the verdict, and the anchor are recorded.
EVENT_GOVERNANCE_DECISION = "governance.decision"

#: Issue #2304 -- emitted whenever an A2A message receipt is anchored in the
#: message-receipt lineage spine. Every inbound/outbound cross-agent message
#: binds ``{message_hash, peer_card_fingerprint, task_uuid, journal_entry_hash}``
#: and the A2A task state it carried. Mirroring the receipt into the chain lets
#: a reviewer prove, from the chain alone, that a cross-agent call happened with
#: the exact inputs claimed, without trusting either agent's logs. Only hashes,
#: the peer fingerprint, the task uuid, and the lifecycle state are recorded --
#: never the message body.
EVENT_A2A_MESSAGE_RECEIPT = "a2a.message_receipt"

#: Issue #2311 -- emitted whenever a typed activity boundary is crossed under the
#: deterministic scheduler, mirroring the run journal's ``activity.result`` entry
#: into the chain. Any agent modality (research, browser/computer-use, data, ops,
#: coding) is dispatched behind a hash-in/hash-out contract; the record binds
#: ``{kind, artifact_hash, evidence_set_hash, terminal_state, reason_code}`` plus
#: the anchoring journal index/hash, so an operator can prove -- from the chain
#: alone -- that a modality-agnostic activity ran with a given evidence set
#: without the record ever exposing the artifact body or the fetched pages. Only
#: hashes, the kind, the terminal state, and the reason code are recorded.
EVENT_ACTIVITY_RESULT = "activity.result"

#: Issue #2362 -- emitted once per sealed verification evidence bundle. A
#: completed task's proof-of-done artefacts (test-runner output, coverage, lint,
#: optional screenshot / recording) are content-addressed and bound into a
#: signed bundle anchored in the evidence lineage spine; this event mirrors the
#: bundle's identity into the HMAC-chained audit log by recording ``{task_id,
#: bundle_hash, item_count, gate_passed, journal_entry_hash}``. A verifier
#: holding the stored blobs can recompute the bundle byte-identically and confirm
#: the evidence is chain-attested rather than merely logged -- never the evidence
#: bytes themselves.
EVENT_EVIDENCE_BUNDLE = "evidence.bundle"

#: Issue #2507 -- emitted whenever an observed provider-side context mutation
#: (compaction boundary or similar opaque state marker surfaced in provider
#: responses) is chained into the run's replay journal. The event mirrors the
#: mutation's content address ``H(kind, before_digest, after_digest,
#: step_index)`` plus the journal head after the entry was chained, so an
#: operator can prove, from the chain alone, that a server-side context
#: rewrite was pinned into the run identity before anything built on the
#: mutated state -- or that a flagged mutation arrived in deterministic mode
#: and the run fails verification closed. Only the kind, the content address,
#: the step index, the flag, and the journal anchor are recorded -- never the
#: mutated context itself.
EVENT_PROVIDER_STATE_MUTATION = "provider.state_mutation"

#: Issue #2358 -- emitted whenever a run's durable work ledger is anchored
#: to its dedicated git ref (``refs/bernstein/work-ledger/<run-id>``). The
#: hash-chained ledger is the resumable task-graph state; this event mirrors
#: each anchor point into the HMAC-chained audit log by recording ``{run_id,
#: head_hash, entry_count, chunk_count, ref, tree_sha}`` plus the previous
#: chain digest. A verifier holding the repository can walk the anchored
#: chain, recompute the head, and confirm the resume point is chain-attested
#: -- the payloads themselves never enter the audit log.
EVENT_WORK_LEDGER_ANCHOR = "work_ledger.anchor"

#: Issue #2359 -- emitted once per checkpointed-retry decision. When a failed
#: task is retried, the scheduler decides warm (resume the recorded native
#: session), fork (branch off the recorded checkpoint), or cold (restart from
#: zero) as a pure function of the adapter's declared capability, the
#: journal-anchored checkpoint reference, and a workspace-hash comparison.
#: The event mirrors ``{task_id, retry_mode, requested_mode, capability,
#: checkpoint_event_hash, checkpoint_journal_index, workspace_match,
#: downgrade_reason, decision_hash, journal_event_hash, journal_entry_hash}``
#: into the chain, so an operator can prove -- from the chain alone -- whether
#: a retry continued a session or restarted cold and why. Only identifiers,
#: hashes, the mode, and the downgrade reason are recorded -- never prompt,
#: gate output, or session content.
EVENT_CHECKPOINT_RETRY = "retry.checkpoint_decision"

#: Issue #2355 -- emitted once per provider-availability routing decision
#: (dispatch-time failover or doctor failover drill). The event records the
#: role, the full fallback chain considered, the recorded probe outcomes, the
#: chosen chain position, the reason the decision fired, and the deterministic
#: decision hash. A verifier holding the same recorded probe set recomputes
#: the routing decision byte-identically and checks it against the chain --
#: two operators replay the same routing.
EVENT_ROUTING_FAILOVER_RECEIPT = "routing.failover_receipt"

#: Issue #2356 -- emitted once per sealed endpoint certification receipt. A
#: conformance run against an OpenAI-compatible endpoint (reachability, chat
#: completion, tool calling, patch format fidelity, timeout behavior, context
#: floor) is bound into an Ed25519-signed receipt anchored in the
#: ``endpoint-certification`` lineage spine run; this event mirrors the seal
#: by recording ``{fingerprint, model, engine, suite_version,
#: transcript_hash, certified_roles, rejected_roles, journal_entry_hash}`` --
#: never the endpoint's responses themselves. Config validation gates
#: merge-critical roles on the receipt, so "certified" is chain-attested
#: rather than a boolean in config.
EVENT_ENDPOINT_CERTIFICATION = "endpoint.certification"

#: Issue #2357 -- emitted whenever the task server accepts a worker mailbox
#: message (``POST /tasks/{id}/messages``). The event records the mailbox
#: journal position (``seq``), the message kind, the sender and its card
#: fingerprint, the body's SHA-256, the mailbox chain ``entry_hash``, and the
#: DLP redaction count -- never the message body. A verifier holding the
#: mailbox journal can recompute every entry and prove the delivered log
#: equals the chain-attested log (see
#: :func:`bernstein.core.communication.task_mailbox.verify_against_chain`).
EVENT_TASK_MAILBOX_MESSAGE = "task.mailbox_message"

#: Issue #2357 -- emitted whenever a claim endpoint hands a task to a worker.
#: The event records the dependency snapshot the claim was granted under: the
#: task id, its declared ``depends_on`` ids (all terminal by construction --
#: the claim API refuses otherwise), the claiming session, the post-claim task
#: version, and which claim path granted it (``next`` / ``by_id`` /
#: ``batch``). Claims become journal entries on the audit chain, so claim
#: eligibility is reconstructable offline instead of remaining an in-memory
#: scheduler decision.
EVENT_TASK_CLAIM_RECEIPT = "task.claim_receipt"

#: Issue #2369 -- emitted once per packaged agent-skill / plugin install.
#: When the bundled ``bernstein-run`` skill (or a plugin checkout a host
#: performed) lands in an agent host's skill directory, the install writes a
#: content-addressed receipt anchored in the ``skills`` lineage spine and
#: mirrors ``{skill_hash, manifest_hash, install_id, spine_anchor, host,
#: scope, dest}`` into the chain. A verifier recomputes the installed tree's
#: content address and checks it against the receipt and the spine, so
#: "installed" is chain-attested rather than a directory listing.
EVENT_PLUGIN_INSTALL_RECEIPT = "plugin.install_receipt"

#: Issue #2369 (tail) -- emitted when a packaged install is superseded by an
#: update. The event binds ``prior_skill_hash -> skill_hash`` so the
#: supersession is chain-verifiable: a verifier walks update receipts newest
#: to oldest and lands on the root ``plugin.install_receipt``, proving which
#: content addresses an installed tree passed through and in what order.
EVENT_PLUGIN_UPDATE_RECEIPT = "plugin.update_receipt"

#: Issue #2369 (tail) -- emitted when a packaged skill passes a multi-host
#: conformance sweep: one skill content address is installed into several
#: agent hosts against one bernstein install and the skill's documented
#: self-check contract is replayed per host. The event binds the shared
#: content address, the per-host pass/fail verdicts, and the lineage-spine
#: anchor of the conformance receipt, so "the skill works from N agent CLIs
#: against one install" is chain-verifiable rather than a transient CI log.
EVENT_PLUGIN_CONFORMANCE_RECEIPT = "plugin.conformance_receipt"

#: Issue #2368 -- emitted for every probe of the nightly adapter conformance
#: canary. The event binds the probed adapter, the discovered upstream
#: version, the conformance verdict, and the content hash of the canary
#: receipt into the HMAC chain, so a canary finding (and the last-green
#: table row it attests) is reconstructable and tamper-evident offline
#: rather than living only in a CI log.
EVENT_ADAPTER_CANARY_RECEIPT = "adapter.canary_receipt"

#: Issue #2610 -- emitted for every adapter admission decision, positive and
#: negative alike. The event binds the adapter, the installed upstream version,
#: the pinned contract's content hash, the deterministic golden-transcript
#: replay fingerprint, the conformance run id, and the capabilities the
#: decision grants or withholds. Recording refusals as first-class events is
#: the point: a ``skip`` conformance verdict that leaves no record reads as
#: silent permission, whereas a chain slice carrying refusal events proves
#: offline both which adapters held spawn authority during a window and why
#: every other one did not.
EVENT_ADAPTER_ADMISSION_RECEIPT = "adapter.admission_receipt"

#: Issue #2663 -- emitted when capability-aware routing selects an adapter for a
#: task. The event binds the chosen adapter, the content-addressed capability
#: profile it presented at dispatch, and the task requirements it satisfied.
#: Recording the presented ``profile_hash`` makes profile drift replay-visible:
#: a declaration that changes between two runs shows up as a hash divergence
#: named by the adapter rather than as unexplained behaviour change.
EVENT_ADAPTER_CAPABILITY_SELECTION = "adapter.capability_selection"

#: Issue #2663 -- emitted when capability-aware routing refuses a task because
#: no candidate adapter's declared profile satisfied its requirements. The event
#: anchors the content-addressed refusal receipt (its hash, the unmet axes, and
#: every candidate considered with the profile hash it presented) into the HMAC
#: chain, so a routing refusal is a signed, reconstructable record rather than a
#: silent fallback to a weaker adapter.
EVENT_ADAPTER_CAPABILITY_REFUSAL = "adapter.capability_refusal"

#: Issue #2367 -- emitted when the orchestrator forcibly reaps an agent
#: process tree.  The event records which platform mechanism delivered the
#: stop (POSIX process-group signalling or Windows process-tree
#: termination), whether the graceful stop was delivered, whether
#: escalation to a force-kill was required, and the grace window that
#: applied.  Reaps stop being an unobservable side effect of supervision:
#: an operator reconstructing a failure window can prove offline which
#: reap path ran and on which platform semantics it relied.
EVENT_PROCESS_REAP_RECEIPT = "process.reap_receipt"

#: Issue #2366 -- emitted whenever a scoped dashboard token is issued or
#: revoked. The event mirrors the signed registry row: the short token id,
#: the token digest, the principal, the scope, and the grant kind -- never
#: the raw token (the registry itself only ever stores the digest). Together
#: with the ``governance.decision`` events the dashboard authz layer
#: records, the chain carries the full life of a dashboard credential:
#: grant, every write it authorized, and revocation.
EVENT_DASHBOARD_TOKEN_GRANT = "dashboard.token_grant"

#: Issue #2365 -- emitted for every operator action on the run review board
#: (approve / request-changes / merge). The signed, principal-named receipt
#: binds the decision to the projection the operator saw (``projection_hash``)
#: and the exact journal head it chained onto (``journal_entry_hash``), so a
#: reviewer can prove from the chain alone that a named principal took the
#: action against that board state without operator override.
EVENT_REVIEW_BOARD_ACTION = "review_board.action"

#: Issue #2352 -- emitted at every lifecycle boundary of a detached run
#: (submit, detach, reattach, daemon restart, complete). The daemon that owns
#: the run is a projection of the durable work ledger; each receipt binds the
#: ledger head at the boundary so a reattaching operator can prove, from the
#: chain alone, that the current ledger is a forward extension of the head
#: they last saw -- nothing happened off the record while they were away.
#: Only the run id, the transition, the ledger head/entry count, and (for
#: reattach/restart boundaries) the ``from_head``/``to_head``/``entries_added``
#: continuity span are recorded -- never goal text or task payloads.
EVENT_RUN_LIFECYCLE = "run.lifecycle"

#: Issue #2364 -- emitted whenever an MCP Tasks-extension run handle is minted
#: for a long-running run. The event carries the handle receipt: the task id,
#: the run id, the projected status, the run journal head, the embedded
#: audit-chain head, the receipt hash, the pinned spec revision, and the
#: ingested W3C trace id (empty when none). A client that polled the handle
#: can prove offline that the task it watched corresponds to the audited run
#: by recomputing the receipt hash from the journal and matching the embedded
#: chain head against this chain.
EVENT_MCP_TASK_HANDLE = "mcp.task_handle"

#: Issue #2363 -- emitted whenever a SPIRE-issued X.509-SVID is bound to an
#: agent card. The event pins the binding's content hash together with the
#: derived SPIFFE ID, the install fingerprint, the card hash, and the leaf
#: SVID's content address -- never the SVID private key. Because the binding's
#: identity is its content hash and that hash is chained here, the mapping
#: between platform identity (the SVID) and card identity is reconstructable
#: and tamper-evident offline: a verifier holding the chain and the install
#: public key can prove after the fact that a card was bound to exactly this
#: SVID and that neither has been altered since.
EVENT_SPIFFE_SVID_BINDING = "spiffe.svid_binding"

#: Issue #2361 -- emitted when an operator approves (or rejects) the
#: requirement set drafted from a spec, before the deterministic compiler
#: turns it into a task graph. The event binds the content-addressed
#: requirement-set hash, the source-spec hash, the requirement count, the
#: compiled graph hash, and the decision into the HMAC chain. The receipt
#: is the plan-approval gate for the spec pipeline: a verifier can prove,
#: from the chain alone, that a given task graph was compiled from a
#: requirement set the operator signed off on -- no requirement line was
#: added or altered after approval without breaking the chain.
EVENT_SPEC_REQUIREMENT_SET = "spec.requirement_set"

#: Issue #2354 -- emitted once per cost-aware dispatch decision. The
#: deterministic decision (admit/halt under USD caps) is a pure function of a
#: hash-pinned price table, the spend ledger, and the caps; this event mirrors
#: the decision's identity into the HMAC chain by recording ``{decision_hash,
#: run_id, task_id, admit, breached_dimension, projected_overrun_usd,
#: price_table_hash, ledger_state_hash, policy_hash, journal_entry_hash}``. A
#: verifier holding the same ledger and price table recomputes the decision
#: byte-identically and checks it against the chain, so a halt names exactly
#: why it fired and two operators replay the same budget decision. Only hashes,
#: the verdict, and the projected overrun are recorded -- never prompt content.
EVENT_COST_DISPATCH_RECEIPT = "cost.dispatch_receipt"

#: Issue #2353 -- emitted whenever a tournament selects a winning attempt.
#: The event mirrors the signed selection receipt: the task id, the tournament
#: receipt hash (spine anchor), the winning attempt hash, the full set of
#: attempt hashes, and the evaluator names that decided the outcome -- never a
#: model judgement (there is none in the decision path). A verifier can join a
#: chain entry to the offline-verifiable receipt and prove which attempt won
#: and why, without re-running the tournament.
EVENT_TOURNAMENT_SELECTION = "tournament.selection"

#: Issue #2352 (AC4) -- emitted once per detached-run task executed on the ssh
#: sandbox backend. The receipt binds the run and task ids, the remote host, the
#: isolated remote worktree the task ran in, the task's exit code, a digest of
#: the per-task isolation marker written into that worktree, and the work-ledger
#: head at execution time. Only non-secret identifiers and hashes are recorded
#: -- never goal text, task payloads, or injected credentials -- so an auditor
#: can prove from the chain alone that each task of a goal ran in its own
#: worktree across the ssh boundary (distinct worktree per task, none lost).
EVENT_RUN_SSH_TASK = "run.ssh_task"

#: Issue #2556 -- emitted whenever a typed ``blocker`` bulletin signal is
#: materialized into a clearance gate, and again at each clearance / expiry
#: transition. Posting a ``blocker`` deterministically projects a clearance
#: task plus injected ``depends_on`` edges onto the open dependent tasks in the
#: blocker's scope; the whole chain (blocker signal -> clearance task + injected
#: edges -> resolution) is sealed as a receipt on the HMAC chain. The event
#: records ``{blocker_content_hash, clearance_task_id, injected_edges,
#: graph_delta_hash, scope_cell_id, deadline, last_state_hash,
#: journal_entry_hash, blocker_entry_hash, resolution (pending/cleared/expired),
#: resolver}``. ``graph_delta_hash`` is a pure function of the recorded detail
#: fields, so a verifier recomputes it byte-identically from the chain entry
#: alone; a resolution entry references the materialization entry hash via
#: ``blocker_entry_hash``. The signal-to-gate map is a pure projection, so two
#: operators replaying the same bulletin journal produce byte-identical gates --
#: strip the deterministic scheduler and this chain and the gate collapses to a
#: logged blocker. See :mod:`bernstein.core.communication.signal_actions`.
EVENT_SIGNAL_GATE_PROJECTION = "signal.gate_projection"

#: Issue #2515 -- emitted for every spawn-time adapter security-floor decision
#: (permit, refusal, or explicit warn-only override). The event binds the
#: probed adapter, the installed upstream version, the minimum-safe floor, the
#: advisory id, the enforcement policy, the floor map's content hash, and the
#: content hash of the sealed refusal receipt into the HMAC chain. Permits
#: carry the verdict too, so a contiguous chain slice proves offline that no
#: below-floor adapter spawn was permitted during a window -- and a mutated
#: floor map is caught because the receipt pins the map's content hash. Strip
#: the chain and the floor-map hash and a refusal degrades to a logged version
#: check; with them the refusal is the tamper-evident proof artefact.
EVENT_ADAPTER_SPAWN_PREFLIGHT = "adapter.spawn_preflight_receipt"

#: Issue #2515 -- emitted by ``bernstein doctor`` when it snapshots the
#: environment's adapter version posture. The event binds a content-addressed
#: posture receipt (each tracked adapter's installed version, floor, advisory
#: id, and floor verdict) and the floor map's content hash into the HMAC
#: chain, so "only floor-satisfying binaries were spawnable in this
#: environment during window X" is provable offline from a contiguous chain
#: slice rather than living only in a console print that an operator may never
#: run.
EVENT_ADAPTER_VERSION_POSTURE = "adapter.version_posture"

#: Issue #2515 -- emitted when the adapter security-floor map is refreshed from
#: a machine-readable advisory feed. The event binds the old and new floor-map
#: content hashes and the data-only diff into the HMAC chain, so a floor bump
#: is an attested event: a reviewer can prove offline which floor map was in
#: force when a spawn-preflight or version-posture receipt (which pin the same
#: content hash) was recorded, before and after the bump.
EVENT_ADAPTER_FLOOR_UPDATE = "adapter.floor_update_receipt"

#: Issue #2514 -- emitted at plan-approval time when the approved goal is
#: compiled into an intent capsule and written to the chain. The event binds
#: ``{task_id, plan_id, run_id, capsule_hash, goal_digest,
#: allowed_action_classes_hash, expiry_ts}`` so a verifier can prove, from the
#: chain alone, that a worker's run was governed by a capsule the operator
#: signed off on -- and that the on-disk capsule bytes still hash to the
#: chain-recorded ``capsule_hash`` (a tampered capsule diverges). Only hashes
#: and identifiers are recorded -- never the goal text (bound by digest).
EVENT_INTENT_CAPSULE = "intent.capsule"

#: Issue #2514 -- emitted when the deterministic drift monitor detects an action
#: class outside the approved capsule and emits a signed escalation receipt. The
#: event mirrors ``{task_id, capsule_hash, verdict_hash, divergent_count,
#: escalation_journal_entry_hash}`` into the chain so an operator can prove, from
#: the chain alone, that a drift escalation was emitted against a named capsule
#: with a given deterministic verdict. The divergent events themselves live in
#: the run journal and the signed escalation receipt; this event records only
#: their identity.
EVENT_INTENT_DRIFT = "intent.drift"

#: Issue #2649 -- emitted when a capsule-governed run's journal is sealed. The
#: event mirrors ``{task_id, run_id, capsule_hash, journal_head, event_count}``
#: into the chain so a verifier has an independent commitment to the journal's
#: END. Without it, any prefix of a valid journal is itself a valid journal:
#: the Merkle chain recomputes from genesis, so a worker can delete the trailing
#: rows that convict it and present a shorter, internally consistent history.
#: The seal is what makes truncation detectable -- a proof that reads only what
#: remains cannot tell what was removed.
EVENT_INTENT_JOURNAL_SEAL = "intent.journal_seal"

#: Issue #2520 -- emitted once per statistical eval gate verdict. The verdict
#: (significant_improvement / non_inferior / insufficient_evidence /
#: significant_regression) is a pure function of the paired 2x2 discordance
#: table, alpha, the non-inferiority margin, and the minimum n; this event
#: mirrors the sealed verdict receipt's identity into the HMAC chain by
#: recording ``{receipt_hash, verdict, suite_content_hash,
#: baseline_result_set_hash, candidate_result_set_hash, n_per_arm, effect,
#: interval_low, interval_high, alpha, min_n_satisfied, journal_entry_hash}``.
#: A verifier holding the same result sets recomputes the verdict and the
#: receipt hash byte-identically, so a promotion decision names exactly the
#: evidence it stood on. Only hashes, the verdict, and the rounded statistics
#: are recorded -- never task prompts or agent output.
EVENT_EVAL_GATE_VERDICT = "eval.gate_verdict"

#: Issue #2520 -- emitted when a significant_regression verdict at canary or
#: default rolls a candidate configuration back. The revocation receipt names
#: the content hashes of the verdict receipts it revokes and the stage the
#: deterministic promotion projection reverts to; this event mirrors that
#: linkage into the HMAC chain by recording ``{receipt_hash,
#: candidate_config_id, revoked_receipt_hashes, reverts_to_stage,
#: reverts_to_config_id, trigger_receipt_hash, journal_entry_hash}``. A
#: verifier folding the receipt chain offline reproduces the identical
#: rollback, so a regression postmortem links the exact receipt that admitted
#: the change to the receipt that revoked it.
EVENT_EVAL_GATE_REVOCATION = "eval.gate_revocation"

#: Issue #2513 -- emitted whenever an egress-relevant decision consults the
#: propagated taint of an artefact. Records ``{target, trust, tainted,
#: decision, closure_size, trust_records}`` so a verifier folding the chain
#: offline reconstructs exactly which artefact's provenance drove the egress
#: verdict and to which lineage records the taint traced.
EVENT_PROVENANCE_TAINT_DECISION = "provenance.taint_decision"

#: Issue #2513 -- emitted whenever an untrusted payload is passed through the
#: quarantined structural parser before anything from it reaches worker
#: context. Records ``{source_content_hash, extracted_fields,
#: withheld_fields}`` so the extraction edge (structured fields kept, free
#: text withheld) is itself anchored into the chain.
EVENT_PROVENANCE_QUARANTINE = "provenance.quarantine"

#: Issue #2544 -- admission events mirror the hash-chained admission ledger's
#: grants, waivers, quarantines, and tag-conformance seals into the HMAC chain.
#: Each event records only the admission row's identity (its ledger
#: ``entry_hash``) plus the resource and the projected head, never the payloads
#: themselves, so an operator can prove from the audit chain alone that a grant
#: was issued (and against which pool, and at which chain head) without carrying
#: the ledger. Two operators replaying the admission ledger derive the identical
#: grant order; these events anchor that order into the tamper-evident log.
EVENT_ADMISSION_GRANT = "admission.grant_receipt"
EVENT_ADMISSION_WAIVER = "admission.waiver_receipt"
EVENT_ADMISSION_QUARANTINE = "admission.quarantine_receipt"
EVENT_ADMISSION_TAG_CONFORMANCE = "admission.tag_conformance_receipt"
#: Issue #2511 -- emitted once per approval card v2 issuance. An approval card
#: is a hash-committed decision record: the operator-visible envelope (action
#: and canonical args digest, bounded reasoning digest, blast-radius impact
#: estimate, rollback procedure with an explicit irreversible marker, and a
#: ``not_after`` expiry) is canonicalised and hashed into ``card_hash``, and
#: this event stores the full envelope plus ``card_hash`` and
#: ``prev_chain_digest``. Because the whole envelope is inside the signed HMAC
#: chain, a verifier can reconstruct exactly the fields shown to the operator
#: and detect any post-hoc mutation: a mutated envelope no longer hashes to the
#: recorded ``card_hash`` and no longer matches the chain HMAC. Strip the chain
#: and the card degrades from a verifiable decision record to a message with a
#: log.
EVENT_APPROVAL_CARD_ISSUED = "chat.approval_card.issued"

#: Issue #2511 -- emitted when an issued approval card is resolved (approve /
#: reject) after the gate has confirmed the echoed ``card_hash`` matches the
#: issued envelope and the decision arrived before ``not_after``. The event
#: binds ``{card_hash, decision, approver, worktree_id, resolved_at}`` so a
#: verifier can join the decision to the exact issued envelope and prove the
#: operator decided against the fields that were hashed, not a divergent view.
EVENT_APPROVAL_CARD_RESOLVED = "chat.approval_card.resolved"

#: Issue #2511 -- emitted when the gate refuses to resolve an approval card:
#: either the echoed ``card_hash`` does not match any issued envelope (a field
#: the operator saw was mutated) or the decision arrived at or after
#: ``not_after`` (expiry is enforced by the chain-side clock regardless of what
#: the chat client still renders). The refusal records ``{card_hash, reason,
#: expected_card_hash, worktree_id}`` so an operator can prove, from the chain
#: alone, that a stale or tampered decision was contained and never executed.
EVENT_APPROVAL_CARD_REFUSED = "chat.approval_card.refused"

#: Issue #2545 -- emitted whenever an input boundary (schedule fire, recipe
#: launch, MCP ``bernstein_run`` / ``bernstein_scenario`` call, or task-server
#: claim) refuses a parameter that fails its declared contract. The event binds
#: the signed refusal receipt's identity into the HMAC chain: the offending
#: field's JSONPath, the declared schema hash, a digest of the rejected value
#: (raw bytes never stored), the boundary, and the content hash of the sealed
#: receipt. Rejection happens strictly before any adapter or model invocation,
#: so a contiguous chain slice proves offline that a malformed fire was refused
#: at zero spend -- and a mutated receipt is caught because the chain pins its
#: content hash. Strip the chain and the receipt degrades to a logged
#: validation error; with them the refusal is the tamper-evident proof artefact.
EVENT_INPUT_REFUSAL = "input.refusal_receipt"

#: Issue #2545 -- emitted once per spawned worker whose runtime context capsule
#: is sealed. The capsule is a content-addressed, Ed25519-signed record of what
#: the worker was given (task id, run id, params hash, worktree, role, budget
#: envelope remaining, dependency state, and the audit chain head at spawn, plus
#: the intent capsule hash when one exists). This event mirrors ``{task_id,
#: run_id, params_hash, capsule_hash, audit_chain_head, intent_capsule_hash}``
#: into the HMAC chain so a verifier holding only the journal and the chain can
#: recompute the capsule byte-identically at the recorded chain position; a
#: context divergence (different params, budget, or chain head than asserted) is
#: caught as a hash mismatch. Only hashes and identifiers are recorded -- never
#: prompt or budget content.
EVENT_CONTEXT_CAPSULE = "context.capsule"
#: Issue #2509 -- emitted once per mission phase advancement (pass or halt). A
#: mission is a ledger-projected multi-day goal; a phase advances only by a
#: mission phase receipt that binds the gate verdict, the evidence bundle
#: hashes it verified, the ledger position, the envelope, and the envelope
#: spend at gate time. This event mirrors ``{mission_id, phase_id, gate_passed,
#: receipt_hash, evidence_bundle_hashes, ledger_seq, envelope, spend_usd,
#: reason, journal_entry_hash}`` into the HMAC chain so a phase pass (or an
#: envelope-exhausted halt) is provable offline from the chain alone. The
#: receipt binding lives as a ``mission.phase_passed`` / ``mission.phase_halted``
#: entry in the work ledger; the projection derives phase state from receipts
#: and ledger entries only, so a phase without a receipt is by definition not
#: passed. Only identifiers, hashes, the verdict, and the spend are recorded --
#: never goal text or task payloads.
EVENT_MISSION_PHASE_RECEIPT = "mission.phase_receipt"

#: Issue #2510 -- emitted once per recurring mission digest fire. A mission
#: digest is a pure deterministic projection of the mission state at a fire
#: instant (see :mod:`bernstein.core.orchestration.mission_digest`); this event
#: anchors that projection in the HMAC chain by recording ``{mission_id,
#: fire_time, digest_hash, receipt_id, mission_status_hash, ledger_head,
#: phases_passed, gates_passed, gates_failed, total_spend_usd, schedule_id,
#: recurrence, fire_graph_hash, journal_entry_hash}`` plus the previous chain
#: digest. The posted chat message embeds ``digest_hash``, so a recipient
#: recomputes the digest from the ledger and proves the message matches the
#: chain-attested receipt; a tampered receipt fails chain verification at its
#: exact position. ``receipt_id`` is the per-fire delivery idempotency key, a
#: pure function of ``(mission_id, fire_time, digest_hash)``, so a restart
#: between fire computation and delivery does not double-post. Only identifiers,
#: hashes, counts, and the spend are recorded -- never goal text or task
#: payloads.
EVENT_MISSION_DIGEST_RECEIPT = "mission.digest_receipt"

#: Issue #2558 -- emitted once per receipt appended to a leaderless MESH claim
#: journal (see :class:`bernstein.core.orchestration.tracker_pipeline.ClaimJournal`).
#: A MESH claim journal is a signed, append-only, Merkle-chained log of every
#: self-claim, release, renewal, expiry, and supersession; the SQLite
#: ``ClaimLedger`` is a deterministic projection (fold) of it rather than the
#: source of truth. This event mirrors ``{kind, tracker, ticket_id, role,
#: claimer_id, node_id, lease_expires_at, prev_entry_hash, journal_entry_hash,
#: supersedes, winner_claimer_id, winner_entry_hash}`` into the HMAC chain so a
#: claim decision -- including a deterministic ``supersede`` naming the winner of
#: a concurrent double-claim -- is provable offline from the chain alone. The
#: receipt binding lives in the journal; the projection derives claim state from
#: receipts only, so a claim without a receipt is by definition not held. Only
#: identifiers and hashes are recorded -- never ticket or workspace contents.
EVENT_CLAIM_JOURNAL_RECEIPT = "cluster.claim_journal_receipt"

#: Issue #2549 -- emitted whenever a per-goal SLA contract is evaluated against
#: chain evidence inside a supervisor tick and found breached. A breach is a
#: signed, offline-verifiable violation receipt (see
#: :mod:`bernstein.core.orchestration.sla_receipt`); this event mirrors the
#: receipt's identity into the HMAC chain by recording ``{contract_id,
#: contract_hash, subject_type, subject_id, tick_instant, breached_axes,
#: requested_action, effective_action, remediation_blocked, receipt_digest}``
#: plus the previous chain digest. Because the receipt embeds the evidence and
#: re-derives its verdict and remediation offline, a verifier holding only the
#: receipt confirms the breach without orchestrator state; this event lets an
#: operator prove, from the audit chain alone, that the breach was detected at a
#: named chain position with a given remediation. Only identifiers, hashes, the
#: breached axes, and the remediation decision are recorded -- never goal text
#: or artifact contents. Evaluation is read-only and never dispatches a task.
EVENT_SLA_VIOLATION = "sla.violation"

#: Durable task suspension receipts (#2552). A park is made durable by two
#: chained receipts plus their journal rows: ``task.suspend_receipt`` binds the
#: suspend journal row's Merkle head (the suspension's identity), the parked
#: workspace hash, and the envelope balance at park time *before any effect
#: runs*; ``task.resume_receipt`` binds the suspend receipt it continued from,
#: the effective continuation mode, and the new workspace hash. The receipt
#: pair is the continuity proof: a verifier holding a copied chain confirms the
#: resumed run continued from exactly the parked workspace hash, or reads the
#: recorded fork/cold downgrade with its reason. Only identifiers, hashes,
#: modes, and spend are recorded -- never prompt content or session state.
EVENT_TASK_SUSPENDED = "task.suspend_receipt"
EVENT_TASK_RESUMED = "task.resume_receipt"

#: Infrastructure-release receipt for a durable suspension (#2552). Every seat
#: return, sandbox teardown, process reap, and envelope-headroom release hangs
#: off the suspend receipt hash: the release row records which resource was
#: freed and the ``suspend_receipt_hash`` it references. A release effect with
#: no matching receipt is rejected upstream (fail closed) -- without the chain
#: there is no suspension, only a dead process.
EVENT_TASK_RESOURCE_RELEASE = "task.suspend_resource_release"


#: Issue #2604 -- emitted once per ``bernstein audit receipt export``. A
#: receipt projects an existing audit-chain range into standard, offline-
#: verifiable envelopes (COSE_Sign1, in-toto/DSSE, RFC 6962 transparency).
#: The event mirrors the projection's identity into the HMAC chain by recording
#: ``{head_sha256, since, until, event_count, receipt_sha256, formats}`` plus
#: the previous chain digest, so the fact that a standard receipt was emitted
#: over a named range (and which head it bound) is itself chain-attested. The
#: receipt subject digest IS the chain ``head_sha256``; only hashes, the window,
#: and the format list are recorded -- never event payloads.
EVENT_AUDIT_RECEIPT_EXPORT = "audit.receipt_export"


#: Issue #2512 -- emitted for every trigger an automation platform fires into
#: the bridge. ``trigger.receipt.issued`` records an admitted trigger;
#: ``trigger.receipt.refused`` records one turned away (bad signature, stale
#: timestamp, or a replayed trigger id). Both carry the same binding digest, so
#: the negative path is as discoverable as the positive one: a refused trigger
#: leaves a signed, chain-anchored record rather than a silent drop. Only the
#: platform label, request path, granted scope, and hashes are recorded --
#: never the trigger body.
EVENT_TRIGGER_RECEIPT_ISSUED = "trigger.receipt.issued"
EVENT_TRIGGER_RECEIPT_REFUSED = "trigger.receipt.refused"

#: Issue #2512 -- emitted for every status callback the bridge hands back to an
#: automation platform. The row records the reported status, the digest of the
#: producing notification event, and the chain head at emission, so a verifier
#: holding only the delivered envelope can establish whether the status the
#: platform acted on equals the status the chain recorded for that run.
EVENT_STATUS_PROOF_EMITTED = "status.proof.emitted"

#: Issue #2612 -- emitted for every outbound-payment authorization attempt under
#: a signed spend mandate. ``payment.authorized`` records an admitted transaction
#: and ``payment.refused`` records one turned away (over the cap, wrong
#: recipient, expired, cumulative exceeded, bad signature, or wrong presence
#: mode). Both mirror the chain-anchored transaction receipt's identity --
#: ``{mandate_hash, receipt_hash, lineage_entry_hash, decision, amount_nanos,
#: currency, recipient, presence_mode}`` plus, for a refusal, ``refusal_reason``
#: -- together with the previous chain digest captured at decision time. The
#: negative path is therefore as discoverable as the positive one: a refused
#: transaction leaves a signed, chain-anchored record rather than a silent drop.
#: Only hashes, the encoded amount, the opaque recipient id, and the decision are
#: recorded -- never a payment credential or settlement secret. Bernstein
#: authorizes and proves; it never moves money.
EVENT_PAYMENT_AUTHORIZED = "payment.authorized"
EVENT_PAYMENT_REFUSED = "payment.refused"


#: Issue #2886 -- emitted for every generic OData v4 system-of-record write-back.
#: A write-back is not a fire-and-forget HTTP call: the event binds ``{connection,
#: entity set, key predicate, ETag observed before the PATCH, content hash of the
#: sent payload, HTTP status, draft-flow flag}`` into the HMAC chain, so an
#: auditor can prove offline -- with ``bernstein audit verify`` and no new verb --
#: that a given change to a given record was made against a specific concurrency
#: token, and can re-hash the sent body and match it against the recorded payload
#: hash. Only identifiers and hashes are recorded; the entity body and any
#: credential are never stored.
EVENT_ODATA_WRITEBACK = "odata.writeback_receipt"


# ---------------------------------------------------------------------------
# AuditChainStore
# ---------------------------------------------------------------------------


class AuditChainStore:
    """Facade over :class:`AuditLog` that exposes the chain head.

    The underlying :class:`AuditLog` already maintains an HMAC chain;
    this class exposes the prior HMAC (the "previous chain digest")
    to callers that want to embed it inside the event payload before
    the HMAC is computed.

    Args:
        audit_dir: Directory in which JSONL log files are written.
        key: Raw HMAC key. When omitted, the underlying ``AuditLog``
            loads or creates a key via the canonical resolver.
        key_path: Optional path override for the HMAC key file.
    """

    def __init__(
        self,
        audit_dir: Path,
        *,
        key: bytes | None = None,
        key_path: Path | None = None,
    ) -> None:
        self._log = AuditLog(audit_dir=audit_dir, key=key, key_path=key_path)
        # Serialise read-prev-then-append so two concurrent attaches
        # never embed the same predecessor in their details payload.
        # The underlying AuditLog also writes to disk under this same
        # lock, keeping the on-disk chain order consistent with the
        # ``prev_chain_digest`` each event embedded.
        # (bot-ack: 3284182792 -- CodeRabbit major.)
        self._append_lock = threading.Lock()

    # -- public surface -----------------------------------------------------

    @property
    def prev_chain_digest(self) -> str:
        """Return the HMAC of the most recent event (the chain head)."""
        # AuditLog tracks _prev_hmac internally; exposing it here gives
        # callers the value to embed inside the next event's payload
        # without breaking the chain (the embedded value is part of the
        # HMAC input, so a downstream verifier sees consistent records).
        return self._log._prev_hmac  # pyright: ignore[reportPrivateUsage]

    def log_with_prev_digest(
        self,
        *,
        event_type: str,
        actor: str,
        resource_type: str,
        resource_id: str,
        details: dict[str, Any],
    ) -> AuditEvent:
        """Embed the prior chain digest into *details* and append the event.

        The read-and-append is performed under a per-store lock so
        two concurrent calls always see distinct ``prev_chain_digest``
        values and the underlying chain stays linear.
        (bot-ack: 3284182792 -- CodeRabbit major.)
        """
        with self._append_lock:
            merged: dict[str, Any] = details.copy()
            merged["prev_chain_digest"] = self.prev_chain_digest
            return self._log.log(
                event_type=event_type,
                actor=actor,
                resource_type=resource_type,
                resource_id=resource_id,
                details=merged,
            )

    def log(
        self,
        *,
        event_type: str,
        actor: str,
        resource_type: str,
        resource_id: str,
        details: dict[str, Any] | None = None,
    ) -> AuditEvent:
        """Append a plain event (no automatic prev_chain_digest embedding)."""
        return self._log.log(
            event_type=event_type,
            actor=actor,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details,
        )

    def query(
        self,
        *,
        event_type: str | None = None,
        actor: str | None = None,
        since: str | None = None,
        until: str | None = None,
        resource_id: str | None = None,
        include_archived: bool = False,
    ) -> list[AuditEvent]:
        """Delegate to the underlying :class:`AuditLog`.

        ``resource_id`` narrows the scan to a single resource's events and lets
        the underlying log skip parsing lines that cannot match, so a
        per-resource lookup does not cost a full pass over the log.

        ``include_archived`` also replays archived ``*.jsonl.gz`` segments, so
        a caller reasoning about linkage across the retention boundary sees
        the same events :meth:`verify` does.
        """
        return self._log.query(
            event_type=event_type,
            actor=actor,
            since=since,
            until=until,
            resource_id=resource_id,
            include_archived=include_archived,
        )

    def scan_verified(
        self,
        cursor: ChainScanCursor | None = None,
        *,
        event_type: str | None = None,
    ) -> ChainScanResult:
        """Delegate to :meth:`AuditLog.scan_verified` (incremental + authenticated).

        Readers that need authenticated rows on a hot path should use this and
        keep the returned cursor: it verifies exactly what it reads while
        costing O(appended bytes) per call rather than O(entire chain) (#2648).
        """
        return self._log.scan_verified(cursor, event_type=event_type)

    def verify(self) -> tuple[bool, list[str]]:
        """Delegate to the underlying :class:`AuditLog`."""
        return self._log.verify()

    def verify_and_query(
        self,
        *,
        event_type: str | None = None,
        actor: str | None = None,
        since: str | None = None,
        until: str | None = None,
        resource_id: str | None = None,
        include_archived: bool = False,
    ) -> tuple[bool, list[str], list[AuditEvent]]:
        """Verify the chain and project matching events from one snapshot.

        :meth:`verify` and :meth:`query` are otherwise two independent reads: a
        concurrent append landing between them lets a caller act on a
        projection the verification never covered (a verify/query TOCTOU).
        Holding the append lock across both reads pins a single snapshot -- the
        events returned are exactly the events that were verified -- because
        chained appends acquire the same lock before touching the log.

        Returns:
            ``(ok, errors, events)``: the verification verdict and its per-entry
            errors alongside the events matching the filters, all read under
            one lock.
        """
        with self._append_lock:
            ok, errors = self._log.verify()
            events = self._log.query(
                event_type=event_type,
                actor=actor,
                since=since,
                until=until,
                resource_id=resource_id,
                include_archived=include_archived,
            )
        return ok, errors, events


# ---------------------------------------------------------------------------
# Event recording helpers (additive)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MultimodalAttachDetails:
    """Structured payload for the ``multimodal.attach`` event."""

    sha256: str
    mime: str
    operator_install_id_sig: str
    worker_id: str
    turn_seq: int
    worktree_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "sha256": self.sha256,
            "mime": self.mime,
            "operator_install_id_sig": self.operator_install_id_sig,
            "worker_id": self.worker_id,
            "turn_seq": self.turn_seq,
            "worktree_id": self.worktree_id,
        }


def record_multimodal_attach(
    *,
    chain: AuditChainStore,
    sha256: str,
    mime: str,
    operator_install_id_sig: str,
    worker_id: str,
    turn_seq: int,
    worktree_id: str,
) -> AuditEvent:
    """Append a ``multimodal.attach`` event into *chain*.

    Args:
        chain: The audit chain store accepting the entry.
        sha256: Hex digest of the attachment bytes (lower-case, 64 chars).
        mime: MIME type as resolved at attach time (e.g. ``image/png``).
        operator_install_id_sig: Operator install fingerprint signature.
            Captured here so a downstream auditor can attribute the
            attach to a known operator install.
        worker_id: Identifier of the worker that consumed the
            attachment.
        turn_seq: Monotonic turn sequence number on the worker.
        worktree_id: Identifier of the worktree the attachment belongs
            to. Cross-worktree resolution is refused by the resolver.

    Returns:
        The recorded :class:`AuditEvent`. The event details payload
        carries every input plus ``prev_chain_digest`` (set to the
        chain head at write time).
    """
    payload = MultimodalAttachDetails(
        sha256=sha256,
        mime=mime,
        operator_install_id_sig=operator_install_id_sig,
        worker_id=worker_id,
        turn_seq=turn_seq,
        worktree_id=worktree_id,
    ).to_dict()
    return chain.log_with_prev_digest(
        event_type=EVENT_MULTIMODAL_ATTACH,
        actor=worker_id,
        resource_type="multimodal_attachment",
        resource_id=sha256,
        details=payload,
    )


@dataclass(frozen=True)
class ComputerUseActionDetails:
    """Structured payload for the ``computer_use.action`` event (#2606)."""

    run_id: str
    action_index: int
    anchor: str
    prev_anchor: str
    observation_hash: str
    screenshot_sha256: str
    dom_digest: str
    action_kind: str
    action_target: str
    action_value_digest: str
    lineage_entry_hash: str
    worker_id: str
    worktree_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "action_index": self.action_index,
            "anchor": self.anchor,
            "prev_anchor": self.prev_anchor,
            "observation_hash": self.observation_hash,
            "screenshot_sha256": self.screenshot_sha256,
            "dom_digest": self.dom_digest,
            "action_kind": self.action_kind,
            "action_target": self.action_target,
            "action_value_digest": self.action_value_digest,
            "lineage_entry_hash": self.lineage_entry_hash,
            "worker_id": self.worker_id,
            "worktree_id": self.worktree_id,
        }


def record_computer_use_action(
    *,
    chain: AuditChainStore,
    run_id: str,
    action_index: int,
    anchor: str,
    prev_anchor: str,
    observation_hash: str,
    screenshot_sha256: str,
    dom_digest: str,
    action_kind: str,
    action_target: str,
    action_value_digest: str,
    lineage_entry_hash: str,
    worker_id: str,
    worktree_id: str,
) -> AuditEvent:
    """Append a ``computer_use.action`` event into *chain* (#2606).

    This is the per-action replay manifest for a third-party browser /
    computer-use agent: it names the CAS blob of the pre-action screenshot, the
    normalised DOM/accessibility digest, and the canonicalised action, so a
    replay can re-derive the observation hash and recompute the action anchor
    without re-running the agent. Mirrors :func:`record_multimodal_attach`.

    Args:
        chain: The audit chain store accepting the entry.
        run_id: Identifier of the computer-use run the action belongs to.
        action_index: Zero-based position of the action in the run.
        anchor: The action anchor
            (``sha256(prev_anchor, observation_hash, action)``), lower-case hex.
        prev_anchor: The prior action's anchor, or the genesis sentinel for the
            first action.
        observation_hash: ``sha256(pre_action_screenshot + dom_digest)``.
        screenshot_sha256: CAS SHA-256 of the pre-action screenshot bytes.
        dom_digest: Normalised DOM/accessibility digest (hex).
        action_kind: The action verb (e.g. ``navigate``, ``type``, ``click``).
        action_target: The action target (URL / selector / element ref).
        action_value_digest: SHA-256 of any typed value; never the raw value.
        lineage_entry_hash: The signed lineage entry hash for this action.
        worker_id: Identifier of the worker fronting the external agent.
        worktree_id: Worktree the run is isolated to.

    Returns:
        The recorded :class:`AuditEvent`. The details payload carries every
        input plus ``prev_chain_digest`` (set to the chain head at write time).
    """
    payload = ComputerUseActionDetails(
        run_id=run_id,
        action_index=action_index,
        anchor=anchor,
        prev_anchor=prev_anchor,
        observation_hash=observation_hash,
        screenshot_sha256=screenshot_sha256,
        dom_digest=dom_digest,
        action_kind=action_kind,
        action_target=action_target,
        action_value_digest=action_value_digest,
        lineage_entry_hash=lineage_entry_hash,
        worker_id=worker_id,
        worktree_id=worktree_id,
    ).to_dict()
    return chain.log_with_prev_digest(
        event_type=EVENT_COMPUTER_USE_ACTION,
        actor=worker_id,
        resource_type="computer_use_action",
        resource_id=anchor,
        details=payload,
    )


def record_sensitive_gate(
    *,
    chain: AuditChainStore,
    task_id: str,
    rule_id: str,
    action: str,
    span_hash: str,
) -> AuditEvent:
    """Append a ``compaction.sensitive_gate`` event into *chain*.

    Args:
        chain: The audit chain store accepting the entry.
        task_id: Task (or session) whose compaction input was gated.
        rule_id: Identifier of the deny rule that fired (e.g.
            ``content.pem-private-key``).
        action: One of ``redacted``, ``refused``, or ``suppressed``.
        span_hash: Hex SHA-256 of the offending span bytes. The hash is
            the only trace of the span -- content is never recorded.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest``
        embedded in its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_COMPACTION_SENSITIVE_GATE,
        actor=task_id,
        resource_type="compaction",
        resource_id=task_id,
        details={
            "task_id": task_id,
            "rule_id": rule_id,
            "action": action,
            "span_hash": span_hash,
        },
    )


@dataclass(frozen=True)
class CostProfileReportDetails:
    """Structured payload for the ``cost.profile_report`` event."""

    report_sha256: str
    ledger_lines_sha256: str
    ledger_first_line_sha256: str
    ledger_last_line_sha256: str
    ledger_line_count: int
    window: str
    artifact_name: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_sha256": self.report_sha256,
            "ledger_lines_sha256": self.ledger_lines_sha256,
            "ledger_first_line_sha256": self.ledger_first_line_sha256,
            "ledger_last_line_sha256": self.ledger_last_line_sha256,
            "ledger_line_count": self.ledger_line_count,
            "window": self.window,
            "artifact_name": self.artifact_name,
        }


def record_cost_profile_report(
    *,
    chain: AuditChainStore,
    report_sha256: str,
    ledger_lines_sha256: str,
    ledger_first_line_sha256: str,
    ledger_last_line_sha256: str,
    ledger_line_count: int,
    window: str,
    artifact_name: str,
    actor: str = "cost",
) -> AuditEvent:
    """Append a ``cost.profile_report`` event into *chain*.

    Args:
        chain: The audit chain store accepting the entry.
        report_sha256: Hex digest of the report's canonical content.
        ledger_lines_sha256: Digest over every ledger line in the
            report's window (newline-joined raw line bytes).
        ledger_first_line_sha256: Digest of the first included ledger
            line (empty when the window is empty).
        ledger_last_line_sha256: Digest of the last included ledger
            line (empty when the window is empty).
        ledger_line_count: Number of ledger lines in the window.
        window: Human window spec the report was computed over
            (for example ``"7d"`` or ``"all"``).
        artifact_name: Content-addressed artifact filename.
        actor: Recorded actor; defaults to ``"cost"`` (the CLI surface).

    Returns:
        The recorded :class:`AuditEvent`. The event details payload
        carries every input plus ``prev_chain_digest`` (set to the
        chain head at write time).
    """
    payload = CostProfileReportDetails(
        report_sha256=report_sha256,
        ledger_lines_sha256=ledger_lines_sha256,
        ledger_first_line_sha256=ledger_first_line_sha256,
        ledger_last_line_sha256=ledger_last_line_sha256,
        ledger_line_count=ledger_line_count,
        window=window,
        artifact_name=artifact_name,
    ).to_dict()
    return chain.log_with_prev_digest(
        event_type=EVENT_COST_PROFILE_REPORT,
        actor=actor,
        resource_type="cost_profile_report",
        resource_id=report_sha256,
        details=payload,
    )


@dataclass(frozen=True)
class EvalAbComparisonDetails:
    """Structured payload for the ``eval.ab_comparison`` event."""

    artifact_sha256: str
    suite_sha256: str
    profile_a_sha256: str
    profile_b_sha256: str
    arm_count: int
    row_count: int
    winner_arm: str
    artifact_name: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_sha256": self.artifact_sha256,
            "suite_sha256": self.suite_sha256,
            "profile_a_sha256": self.profile_a_sha256,
            "profile_b_sha256": self.profile_b_sha256,
            "arm_count": self.arm_count,
            "row_count": self.row_count,
            "winner_arm": self.winner_arm,
            "artifact_name": self.artifact_name,
        }


def record_eval_ab_comparison(
    *,
    chain: AuditChainStore,
    artifact_sha256: str,
    suite_sha256: str,
    profile_a_sha256: str,
    profile_b_sha256: str,
    arm_count: int,
    row_count: int,
    winner_arm: str,
    artifact_name: str,
    actor: str = "eval",
) -> AuditEvent:
    """Append an ``eval.ab_comparison`` event into *chain*.

    Args:
        chain: The audit chain store accepting the entry.
        artifact_sha256: Hex digest of the artifact's canonical content.
        suite_sha256: Hex digest of the eval suite file bytes.
        profile_a_sha256: Addendum hash of the honest pair's A arm.
        profile_b_sha256: Addendum hash of the honest pair's B arm.
        arm_count: Number of arms in the comparison (2 or 3).
        row_count: Number of per-task run rows in the artifact.
        winner_arm: Declared winner arm name, ``tie``, or
            ``incomparable``.
        artifact_name: Content-addressed artifact filename.
        actor: Recorded actor; defaults to ``"eval"`` (the CLI surface).

    Returns:
        The recorded :class:`AuditEvent`. The event details payload
        carries every input plus ``prev_chain_digest`` (set to the
        chain head at write time).
    """
    payload = EvalAbComparisonDetails(
        artifact_sha256=artifact_sha256,
        suite_sha256=suite_sha256,
        profile_a_sha256=profile_a_sha256,
        profile_b_sha256=profile_b_sha256,
        arm_count=arm_count,
        row_count=row_count,
        winner_arm=winner_arm,
        artifact_name=artifact_name,
    ).to_dict()
    return chain.log_with_prev_digest(
        event_type=EVENT_EVAL_AB_COMPARISON,
        actor=actor,
        resource_type="eval_ab_comparison",
        resource_id=artifact_sha256,
        details=payload,
    )


@dataclass(frozen=True)
class MemoryWriteDetails:
    """Structured payload for the ``memory.write`` event."""

    entry_hash: str
    source_hash: str
    scope: str
    namespace: str
    run_id: str
    step_id: str
    kind: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_hash": self.entry_hash,
            "source_hash": self.source_hash,
            "scope": self.scope,
            "namespace": self.namespace,
            "run_id": self.run_id,
            "step_id": self.step_id,
            "kind": self.kind,
        }


def record_memory_write(
    *,
    chain: AuditChainStore,
    entry_hash: str,
    source_hash: str,
    scope: str,
    namespace: str,
    actor: str,
    run_id: str,
    step_id: str,
    kind: str,
) -> AuditEvent:
    """Append a ``memory.write`` event into *chain*.

    Mirrors one memory-chain append into the HMAC-chained audit log so an
    operator can reconstruct, from the audit chain alone, that a fact was
    written by ``actor`` at a time and anchored to a lineage-spine entry.
    Only hashes and identifiers are recorded -- never the remembered
    claim content.

    Args:
        chain: The audit chain store accepting the entry.
        entry_hash: The memory-chain record's content-addressed entry
            hash.
        source_hash: Lineage-spine ``entry_hash`` the record anchors to.
        scope: Identity scope (``user`` / ``agent`` / ``run`` / ``app``).
        namespace: Chain key within the scope.
        actor: Producing agent / actor identifier.
        run_id: Originating orchestration run id.
        step_id: Originating step / tool-call id.
        kind: ``write`` or ``tombstone``.

    Returns:
        The recorded :class:`AuditEvent`. The event details payload
        carries every input plus ``prev_chain_digest`` (set to the chain
        head at write time).
    """
    payload = MemoryWriteDetails(
        entry_hash=entry_hash,
        source_hash=source_hash,
        scope=scope,
        namespace=namespace,
        run_id=run_id,
        step_id=step_id,
        kind=kind,
    ).to_dict()
    return chain.log_with_prev_digest(
        event_type=EVENT_MEMORY_WRITE,
        actor=actor,
        resource_type="memory_write",
        resource_id=entry_hash,
        details=payload,
    )


@dataclass(frozen=True)
class SkillInstallReceiptDetails:
    """Structured payload for the ``skill.install_receipt`` event."""

    skill_hash: str
    manifest_hash: str
    install_id: str
    spine_anchor: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_hash": self.skill_hash,
            "manifest_hash": self.manifest_hash,
            "install_id": self.install_id,
            "spine_anchor": self.spine_anchor,
        }


def record_skill_install_receipt(
    *,
    chain: AuditChainStore,
    skill_hash: str,
    manifest_hash: str,
    install_id: str,
    spine_anchor: str,
    actor: str = "skill_provenance",
) -> AuditEvent:
    """Append a ``skill.install_receipt`` event into *chain*.

    Args:
        chain: The audit chain store accepting the entry.
        skill_hash: Content hash of the installed skill (``sha256:<hex>``).
        manifest_hash: SHA-256 of the authorising catalog manifest.
        install_id: Per-install unique identifier tying this event to the
            lockfile row and the receipt anchor.
        spine_anchor: Entry hash of the receipt row in the install lineage
            spine; a verifier holding the spine can recompute it.
        actor: Recorded actor; defaults to ``"skill_provenance"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded
        in its details payload.
    """
    payload = SkillInstallReceiptDetails(
        skill_hash=skill_hash,
        manifest_hash=manifest_hash,
        install_id=install_id,
        spine_anchor=spine_anchor,
    ).to_dict()
    return chain.log_with_prev_digest(
        event_type=EVENT_SKILL_INSTALL_RECEIPT,
        actor=actor,
        resource_type="skill_install_receipt",
        resource_id=skill_hash,
        details=payload,
    )


def record_skill_usage(
    *,
    chain: AuditChainStore,
    skill_hash: str,
    run_id: str,
    journal_head: str,
    actor: str = "skill_provenance",
) -> AuditEvent:
    """Append a ``skill.usage`` event into *chain*.

    Args:
        chain: The audit chain store accepting the entry.
        skill_hash: Content hash of the skill that participated in the run.
        run_id: The run identifier (spine run id).
        journal_head: The run's journal head (spine head hash) at the moment
            the skill participated.
        actor: Recorded actor; defaults to ``"skill_provenance"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded
        in its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_SKILL_USAGE,
        actor=actor,
        resource_type="skill_usage",
        resource_id=skill_hash,
        details={
            "skill_hash": skill_hash,
            "run_id": run_id,
            "journal_head": journal_head,
        },
    )


@dataclass(frozen=True)
class SkillVerificationRefusalDetails:
    """Structured payload for the ``skill.verification_refusal`` event.

    A refusal is recorded whenever an install, doctor check, or spawn-side
    injection declines a skill because a transparency proof failed or a signed
    revocation covers it. The receipt is chain-anchored and forensically
    reconstructable: an operator can prove, from the audit chain alone, that a
    known-bad version was refused, when, and why.
    """

    skill_id: str
    stage: str
    reason_code: str
    detail: str
    version: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "stage": self.stage,
            "reason_code": self.reason_code,
            "detail": self.detail,
            "version": self.version,
        }


def record_skill_verification_refusal(
    *,
    chain: AuditChainStore,
    skill_id: str,
    stage: str,
    reason_code: str,
    detail: str,
    version: str = "",
    actor: str = "skill_catalog",
) -> AuditEvent:
    """Append a ``skill.verification_refusal`` event into *chain*.

    Args:
        chain: The audit chain store accepting the entry.
        skill_id: Catalog id of the refused skill.
        stage: Where the refusal happened -- ``"install"``, ``"doctor"``, or
            ``"spawn"``.
        reason_code: Machine-readable reason, e.g. ``"revoked"``,
            ``"inclusion_proof_failed"``, ``"consistency_proof_failed"``.
        detail: Human-readable explanation captured at refusal time.
        version: The refused skill version, when known.
        actor: Recorded actor; defaults to ``"skill_catalog"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded.
    """
    payload = SkillVerificationRefusalDetails(
        skill_id=skill_id,
        stage=stage,
        reason_code=reason_code,
        detail=detail,
        version=version,
    ).to_dict()
    return chain.log_with_prev_digest(
        event_type=EVENT_SKILL_VERIFICATION_REFUSAL,
        actor=actor,
        resource_type="skill_verification_refusal",
        resource_id=skill_id,
        details=payload,
    )


@dataclass(frozen=True)
class MandateConsentReceiptDetails:
    """Structured payload for the ``mandate.consent_receipt`` event."""

    mandate_hash: str
    intent_hash: str
    authorized_tool_calls_hash: str
    settlement_ref_hash: str
    journal_entry_hash: str
    task_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "mandate_hash": self.mandate_hash,
            "intent_hash": self.intent_hash,
            "authorized_tool_calls_hash": self.authorized_tool_calls_hash,
            "settlement_ref_hash": self.settlement_ref_hash,
            "journal_entry_hash": self.journal_entry_hash,
            "task_id": self.task_id,
        }


def record_mandate_consent_receipt(
    *,
    chain: AuditChainStore,
    mandate_hash: str,
    intent_hash: str,
    authorized_tool_calls_hash: str,
    settlement_ref_hash: str,
    journal_entry_hash: str,
    task_id: str,
    actor: str = "payment_mandate",
) -> AuditEvent:
    """Append a ``mandate.consent_receipt`` event into *chain*.

    Mirrors one journal-anchored consent receipt into the HMAC-chained audit
    log so an operator can prove, from the audit chain alone, that a payment
    was authorized by a specific intent. Only hashes and the settlement
    reference digest are recorded -- never a payment credential.

    Args:
        chain: The audit chain store accepting the entry.
        mandate_hash: Content hash of the signed cart mandate.
        intent_hash: Content hash of the authorising intent mandate.
        authorized_tool_calls_hash: Content hash of the authorized tool-call
            set.
        settlement_ref_hash: Digest of the bound HTTP 402 settlement
            reference.
        journal_entry_hash: The lineage-spine entry hash anchoring the
            receipt; a verifier holding the spine can recompute it.
        task_id: Task the settlement was attributed to.
        actor: Recorded actor; defaults to ``"payment_mandate"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded
        in its details payload.
    """
    payload = MandateConsentReceiptDetails(
        mandate_hash=mandate_hash,
        intent_hash=intent_hash,
        authorized_tool_calls_hash=authorized_tool_calls_hash,
        settlement_ref_hash=settlement_ref_hash,
        journal_entry_hash=journal_entry_hash,
        task_id=task_id,
    ).to_dict()
    return chain.log_with_prev_digest(
        event_type=EVENT_MANDATE_CONSENT_RECEIPT,
        actor=actor,
        resource_type="mandate_consent_receipt",
        resource_id=mandate_hash,
        details=payload,
    )


def record_mandate_revocation(
    *,
    chain: AuditChainStore,
    mandate_hash: str,
    reason: str,
    actor: str = "payment_mandate",
) -> AuditEvent:
    """Append a ``mandate.revocation`` event into *chain*.

    Args:
        chain: The audit chain store accepting the entry.
        mandate_hash: The revoked mandate (intent or cart) hash.
        reason: Human-readable revocation reason.
        actor: Recorded actor; defaults to ``"payment_mandate"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded
        in its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_MANDATE_REVOCATION,
        actor=actor,
        resource_type="mandate_revocation",
        resource_id=mandate_hash,
        details={
            "mandate_hash": mandate_hash,
            "reason": reason,
        },
    )


@dataclass(frozen=True)
class PaymentReceiptDetails:
    """Structured payload mirrored for a ``payment.authorized`` / ``payment.refused`` event.

    Carries only the transaction receipt's identity and encoded scope -- hashes,
    the string-encoded amount, the opaque recipient id, the presence mode, the
    decision, and (for a refusal) the closed-enum reason. Never a credential.
    """

    mandate_hash: str
    receipt_hash: str
    lineage_entry_hash: str
    amount_nanos: str
    currency: str
    recipient: str
    presence_mode: str
    decision: str
    refusal_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "mandate_hash": self.mandate_hash,
            "receipt_hash": self.receipt_hash,
            "lineage_entry_hash": self.lineage_entry_hash,
            "amount_nanos": self.amount_nanos,
            "currency": self.currency,
            "recipient": self.recipient,
            "presence_mode": self.presence_mode,
            "decision": self.decision,
        }
        if self.refusal_reason is not None:
            out["refusal_reason"] = self.refusal_reason
        return out


def record_payment_authorized(
    *,
    chain: AuditChainStore,
    mandate_hash: str,
    receipt_hash: str,
    lineage_entry_hash: str,
    amount_nanos: str,
    currency: str,
    recipient: str,
    presence_mode: str,
    actor: str = "payment_mandate",
) -> AuditEvent:
    """Append a ``payment.authorized`` event mirroring a transaction receipt.

    The event binds the receipt's identity into the HMAC chain so a verifier
    holding only the chain can prove an outbound payment was authorized under a
    specific mandate, at a named chain position, for the recorded scope. Only
    hashes and encoded scope are recorded -- never a payment credential.

    Args:
        chain: The audit chain store accepting the entry.
        mandate_hash: Content hash of the signed spend mandate.
        receipt_hash: Content hash of the transaction receipt body.
        lineage_entry_hash: Lineage entry hash anchoring the receipt artefact.
        amount_nanos: String-encoded integer nano-unit amount.
        currency: ISO-4217-style uppercase currency code.
        recipient: Opaque payee id.
        presence_mode: Mode that authorized the transaction.
        actor: Recorded actor; defaults to ``"payment_mandate"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded.
    """
    payload = PaymentReceiptDetails(
        mandate_hash=mandate_hash,
        receipt_hash=receipt_hash,
        lineage_entry_hash=lineage_entry_hash,
        amount_nanos=amount_nanos,
        currency=currency,
        recipient=recipient,
        presence_mode=presence_mode,
        decision="authorized",
    ).to_dict()
    return chain.log_with_prev_digest(
        event_type=EVENT_PAYMENT_AUTHORIZED,
        actor=actor,
        resource_type="payment_receipt",
        resource_id=receipt_hash,
        details=payload,
    )


def record_payment_refused(
    *,
    chain: AuditChainStore,
    mandate_hash: str,
    receipt_hash: str,
    lineage_entry_hash: str,
    amount_nanos: str,
    currency: str,
    recipient: str,
    presence_mode: str,
    refusal_reason: str,
    actor: str = "payment_mandate",
) -> AuditEvent:
    """Append a ``payment.refused`` event mirroring a refused transaction receipt.

    A refusal is a first-class receipt: the same binding as an authorization
    plus a closed-enum ``refusal_reason``, so a denied attempt is as
    reconstructable from the chain as an approved one.

    Args:
        chain: The audit chain store accepting the entry.
        mandate_hash: Content hash of the signed spend mandate refused against.
        receipt_hash: Content hash of the refusal receipt body.
        lineage_entry_hash: Lineage entry hash anchoring the receipt artefact.
        amount_nanos: String-encoded integer nano-unit amount that was refused.
        currency: ISO-4217-style uppercase currency code.
        recipient: Opaque payee id.
        presence_mode: Presence mode of the mandate refused against.
        refusal_reason: Closed-enum reason string.
        actor: Recorded actor; defaults to ``"payment_mandate"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded.
    """
    payload = PaymentReceiptDetails(
        mandate_hash=mandate_hash,
        receipt_hash=receipt_hash,
        lineage_entry_hash=lineage_entry_hash,
        amount_nanos=amount_nanos,
        currency=currency,
        recipient=recipient,
        presence_mode=presence_mode,
        decision="refused",
        refusal_reason=refusal_reason,
    ).to_dict()
    return chain.log_with_prev_digest(
        event_type=EVENT_PAYMENT_REFUSED,
        actor=actor,
        resource_type="payment_receipt",
        resource_id=receipt_hash,
        details=payload,
    )


@dataclass(frozen=True)
class ThreadApprovalDetails:
    """Structured payload for the ``thread.approval`` event."""

    run_id: str
    journal_index: int
    event_hash: str
    decision: str
    operator_install_id_sig: str
    worktree_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "journal_index": self.journal_index,
            "event_hash": self.event_hash,
            "decision": self.decision,
            "operator_install_id_sig": self.operator_install_id_sig,
            "worktree_id": self.worktree_id,
        }


def record_thread_approval(
    *,
    chain: AuditChainStore,
    run_id: str,
    journal_index: int,
    event_hash: str,
    decision: str,
    operator_install_id_sig: str,
    worktree_id: str,
) -> AuditEvent:
    """Append a ``thread.approval`` event into *chain*.

    An approval issued over the live event stream is itself a signed
    record: it anchors the operator's decision to the exact run journal
    entry the stream projected at decision time, so a verifier can prove
    the approval was made against the executed thread (AC4).

    Args:
        chain: The audit chain store accepting the entry.
        run_id: The run whose journal the operator was watching.
        journal_index: 0-based journal index of the entry under approval.
        event_hash: The journal entry's Merkle ``event_hash`` -- the chain
            link that ties the decision to the byte-identical executed row.
        decision: One of ``approve`` or ``reject``.
        operator_install_id_sig: Operator install fingerprint signature,
            recorded as the actor so the approval attributes to a known
            operator install.
        worktree_id: Identifier of the worktree the approval is bound to.

    Returns:
        The recorded :class:`AuditEvent`. The details payload carries every
        input plus ``prev_chain_digest`` (the chain head at write time).
    """
    payload = ThreadApprovalDetails(
        run_id=run_id,
        journal_index=journal_index,
        event_hash=event_hash,
        decision=decision,
        operator_install_id_sig=operator_install_id_sig,
        worktree_id=worktree_id,
    ).to_dict()
    return chain.log_with_prev_digest(
        event_type=EVENT_THREAD_APPROVAL,
        actor=operator_install_id_sig,
        resource_type="thread_approval",
        resource_id=run_id,
        details=payload,
    )


def record_otel_projection(
    *,
    chain: AuditChainStore,
    run_id: str,
    journal_head: str,
    trace_id: str,
    span_count: int,
    projection_sha256: str,
    actor: str = "otel_projection",
) -> AuditEvent:
    """Append an ``otel.projection`` event into *chain*.

    Binds a signed OTel span set to the run journal it projects: a verifier
    holding the journal reprojects byte-identically and confirms the exported
    spans are a faithful projection of the chain rather than free-standing
    telemetry.

    Args:
        chain: The audit chain store accepting the entry.
        run_id: The run identifier (journal run id).
        journal_head: The run's journal head hash the projection anchors to.
        trace_id: The OTLP trace id derived from the run's first entry hash.
        span_count: Number of projected spans.
        projection_sha256: SHA-256 of the canonical signed span set.
        actor: Recorded actor; defaults to ``"otel_projection"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded
        in its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_OTEL_PROJECTION,
        actor=actor,
        resource_type="otel_projection",
        resource_id=trace_id,
        details={
            "run_id": run_id,
            "journal_head": journal_head,
            "trace_id": trace_id,
            "span_count": span_count,
            "projection_sha256": projection_sha256,
        },
    )


@dataclass(frozen=True)
class ForkSnapshotDetails:
    """Structured payload for the ``replay.fork_snapshot`` event (#2295)."""

    parent_run_id: str
    fork_step: int
    snapshot_sha: str
    new_run_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "parent_run_id": self.parent_run_id,
            "fork_step": self.fork_step,
            "snapshot_sha": self.snapshot_sha,
            "new_run_id": self.new_run_id,
        }


def record_fork_snapshot(
    *,
    chain: AuditChainStore,
    parent_run_id: str,
    fork_step: int,
    snapshot_sha: str,
    new_run_id: str,
    actor: str = "replay_fork",
) -> AuditEvent:
    """Append a ``replay.fork_snapshot`` event into *chain* (#2295).

    Args:
        chain: The audit chain store accepting the entry.
        parent_run_id: The run that was forked from.
        fork_step: The journal step index the fork branched at.
        snapshot_sha: The content-addressed snapshot commit sha the child
            worktree was resumed from. A verifier holding the parent
            journal and the snapshot ref can confirm the fork point.
        new_run_id: The child run id the fork produced.
        actor: Recorded actor; defaults to ``"replay_fork"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded
        in its details payload.
    """
    payload = ForkSnapshotDetails(
        parent_run_id=parent_run_id,
        fork_step=fork_step,
        snapshot_sha=snapshot_sha,
        new_run_id=new_run_id,
    ).to_dict()
    return chain.log_with_prev_digest(
        event_type=EVENT_FORK_SNAPSHOT,
        actor=actor,
        resource_type="fork_snapshot",
        resource_id=snapshot_sha,
        details=payload,
    )


def record_gate_adjudication(
    *,
    chain: AuditChainStore,
    run_id: str,
    inputs_hash: str,
    rubric_hash: str,
    panel_config_hash: str,
    final_verdict: str,
    journal_entry_hash: str,
    actor: str = "adjudication",
) -> AuditEvent:
    """Append a ``gate.adjudication`` event into *chain*.

    Mirrors a signed maker-checker / judge-panel gate verdict into the HMAC
    chain: the event binds the inputs, rubric, and panel hashes to the record's
    journal anchor, so a verifier can confirm from the chain alone that a gate
    verdict was made against the claimed inputs (AC4). Only hashes and the
    anchor are recorded -- never the raw diff or rubric content.

    Args:
        chain: The audit chain store accepting the entry.
        run_id: The run whose journal the record anchors to.
        inputs_hash: ``sha256:`` hash of the inputs the panel saw.
        rubric_hash: ``sha256:`` hash of the rubric the panel applied.
        panel_config_hash: ``sha256:`` hash of the independent panel config.
        final_verdict: The aggregated terminal verdict (``pass`` / ``fail``).
        journal_entry_hash: The lineage-spine anchor over the record bytes.
        actor: Recorded actor; defaults to ``"adjudication"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_GATE_ADJUDICATION,
        actor=actor,
        resource_type="gate_adjudication",
        resource_id=run_id,
        details={
            "run_id": run_id,
            "inputs_hash": inputs_hash,
            "rubric_hash": rubric_hash,
            "panel_config_hash": panel_config_hash,
            "final_verdict": final_verdict,
            "journal_entry_hash": journal_entry_hash,
        },
    )


def record_review_receipt(
    *,
    chain: AuditChainStore,
    pr_url: str,
    issue_hash: str,
    plan_hash: str,
    journal_head: str,
    diff_hash: str,
    verdict: str,
    journal_entry_hash: str,
    actor: str = "review_receipt",
) -> AuditEvent:
    """Append a ``review.receipt`` event into *chain*.

    Mirrors a signed, spine-anchored review receipt into the HMAC-chained audit
    log so an operator can prove, from the chain alone, that a review receipt
    binding the issue to the diff was emitted for a PR without operator
    override. Only hashes, the verdict, and the PR url are recorded -- never the
    diff or issue body.

    Args:
        chain: The audit chain store accepting the entry.
        pr_url: The pull request the receipt covers.
        issue_hash: Content hash of the reviewed issue body.
        plan_hash: Content hash of the worker's plan.
        journal_head: The run journal Merkle head (every executed tool call).
        diff_hash: Content hash of the PR diff.
        verdict: The review verdict.
        journal_entry_hash: The review-spine entry hash anchoring the receipt.
        actor: Recorded actor; defaults to ``"review_receipt"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded in
        its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_REVIEW_RECEIPT,
        actor=actor,
        resource_type="review_receipt",
        resource_id=pr_url,
        details={
            "pr_url": pr_url,
            "issue_hash": issue_hash,
            "plan_hash": plan_hash,
            "journal_head": journal_head,
            "diff_hash": diff_hash,
            "verdict": verdict,
            "journal_entry_hash": journal_entry_hash,
        },
    )


def record_mcp_stateless_call(
    *,
    chain: AuditChainStore,
    run_id: str,
    method: str,
    call_index: int,
    trace_id: str,
    span_id: str,
    journal_head: str,
    cache_content_hash: str = "",
) -> AuditEvent:
    """Append an ``mcp.stateless_call`` event into *chain*.

    Anchors a stateless MCP call's cross-call continuity in the audit chain
    rather than a session store: the stateless spec removes the handshake and
    ``Mcp-Session-Id``, so ordering must live somewhere verifiable. The event
    binds the call's content-derived W3C trace/span ids to the run journal head
    it was recorded against, so a verifier recomputes ordering from verified
    chain entries instead of trusting a session id (AC4).

    Args:
        chain: The audit chain store accepting the entry.
        run_id: The run whose journal recorded the call.
        method: The MCP method (e.g. ``tools/call``).
        call_index: 0-based ordered index of the call within the run.
        trace_id: The content-derived W3C trace id (run-scoped).
        span_id: The content-derived W3C span id (call-scoped).
        journal_head: The run journal head hash the call was recorded against.
        cache_content_hash: On a cache hit, the content hash of the producing
            run's value; empty for a miss (AC5).

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded
        in its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_MCP_STATELESS_CALL,
        actor="mcp_stateless_core",
        resource_type="mcp_stateless_call",
        resource_id=span_id,
        details={
            "run_id": run_id,
            "method": method,
            "call_index": call_index,
            "trace_id": trace_id,
            "span_id": span_id,
            "journal_head": journal_head,
            "cache_content_hash": cache_content_hash,
        },
    )


def reconstruct_mcp_call_order(*, chain: AuditChainStore, run_id: str) -> list[dict[str, Any]]:
    """Rebuild a run's ordered MCP call sequence purely from chain entries.

    With the protocol session stores deleted (issue #2506) the audit chain is
    the only authority on MCP call ordering. Verification and projection read
    one locked snapshot, so a tampered ``mcp.stateless_call`` entry fails at
    exactly that entry (the underlying verifier names the file and line) and
    the projected events are exactly the events that were verified -- a
    concurrent append cannot slip between the two reads; the surviving entries
    are then projected into their recorded ``call_index`` order and the
    sequence is checked for gaps and duplicates.

    Args:
        chain: The audit chain store holding the run's entries.
        run_id: The run whose call ordering to reconstruct.

    Returns:
        The ordered list of ``mcp.stateless_call`` detail payloads for the
        run (empty when the run recorded no MCP calls).

    Raises:
        ValueError: When chain verification fails (the message carries the
            verifier's per-entry errors) or when the recorded ``call_index``
            sequence has a gap or duplicate.
    """
    ok, errors, events = chain.verify_and_query(event_type=EVENT_MCP_STATELESS_CALL)
    if not ok:
        msg = "audit chain verification failed: " + "; ".join(errors)
        raise ValueError(msg)

    details = [event.details for event in events if str(event.details.get("run_id", "")) == run_id]
    ordered = sorted(details, key=lambda d: int(d.get("call_index", -1)))
    indexes = [int(d.get("call_index", -1)) for d in ordered]
    if indexes != list(range(len(indexes))):
        msg = f"mcp.stateless_call ordering for run {run_id!r} is not contiguous: call_index sequence {indexes}"
        raise ValueError(msg)
    return ordered


def record_subagent_delegation(
    *,
    chain: AuditChainStore,
    run_id: str,
    node_name: str,
    target: str,
    node_hash: str,
    result_content_hash: str,
    journal_index: int,
    journal_event_hash: str,
    tier: str = "interactive",
    actor: str = "subagent_delegation",
) -> AuditEvent:
    """Append a ``subagent.delegation`` event into *chain*.

    Anchors one leaf of a deterministic outer plan that delegated mechanical
    execution to a native subagent. The ``node_hash`` is a pure function of the
    outer plan, so it is byte-identical across replays; the
    ``result_content_hash`` fixes the (stochastic) native result the delegation
    produced, and ``journal_index`` / ``journal_event_hash`` tie both to the
    exact run journal entry the delegation was anchored at. A verifier holding
    the chain can prove the cross-worker DAG crossed this boundary without the
    record ever exposing the native result payload.

    Args:
        chain: The audit chain store accepting the entry.
        run_id: The run whose journal the delegation was anchored into.
        node_name: The outer-plan node name (the delegation leaf).
        target: The native subagent target (e.g. ``claude`` or ``codex``).
        node_hash: The deterministic plan-node hash (replay-invariant).
        result_content_hash: SHA-256 of the canonical native result payload.
        journal_index: 0-based journal index of the anchoring entry.
        journal_event_hash: The anchoring journal entry's Merkle ``event_hash``.
        tier: Execution tier -- ``batch`` for non-interactive fan-out, else
            ``interactive``.
        actor: Recorded actor; defaults to ``"subagent_delegation"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded in
        its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_SUBAGENT_DELEGATION,
        actor=actor,
        resource_type="subagent_delegation",
        resource_id=node_name,
        details={
            "run_id": run_id,
            "node_name": node_name,
            "target": target,
            "node_hash": node_hash,
            "result_content_hash": result_content_hash,
            "journal_index": journal_index,
            "journal_event_hash": journal_event_hash,
            "tier": tier,
        },
    )


def record_schedule_fire_projection(
    *,
    chain: AuditChainStore,
    schedule_id: str,
    fire_time: int,
    last_state_hash: str,
    graph_hash: str,
    journal_entry_hash: str,
    trigger_input_hash: str = "",
    recurrence: str = "",
    actor: str = "schedule_projection",
) -> AuditEvent:
    """Append a ``schedule.fire_projection`` event into *chain* (#2302).

    Anchors one recurring-goal fire in the HMAC chain as a projection:
    ``(schedule_id, fire_time, last_state_hash)`` are the pure inputs and
    ``graph_hash`` is the canonical task-graph hash they project onto. A
    verifier holding the schedule can re-run the projection at ``fire_time``
    and confirm the recorded ``graph_hash`` byte-identically, so the fire is
    a hash rather than a trigger. The ``journal_entry_hash`` binds the fire
    to the lineage-spine entry the projection was sealed into, and
    ``trigger_input_hash`` binds the exact webhook / file-change event for a
    trigger-driven fire (empty for a plain cron / RRULE fire).

    Args:
        chain: The audit chain store accepting the entry.
        schedule_id: Stable schedule identifier.
        fire_time: Integer Unix epoch of the canonical fire instant.
        last_state_hash: Digest of the ``last_state`` folded into the
            projection (``"genesis"`` for the first fire of a schedule).
        graph_hash: The canonical task-graph hash the inputs project onto.
        journal_entry_hash: The lineage-spine entry hash the projection was
            sealed into; a verifier holding the spine can recompute it.
        trigger_input_hash: For a webhook / file-change trigger, the hash of
            the trigger event bound into the projection; empty otherwise.
        recurrence: Canonical recurrence rule (``cron:`` / ``RRULE:``) that
            produced the fire instant; empty when none was declared.
        actor: Recorded actor; defaults to ``"schedule_projection"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded
        in its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_SCHEDULE_FIRE_PROJECTION,
        actor=actor,
        resource_type="schedule_fire_projection",
        resource_id=schedule_id,
        details={
            "schedule_id": schedule_id,
            "fire_time": fire_time,
            "last_state_hash": last_state_hash,
            "graph_hash": graph_hash,
            "journal_entry_hash": journal_entry_hash,
            "trigger_input_hash": trigger_input_hash,
            "recurrence": recurrence,
        },
    )


class ClearanceResolutionRefusal(ValueError):
    """Typed refusal for a clearance resolution outside the allowed vocabulary.

    Raised at every mutation boundary that would otherwise persist or sign an
    unrecognised resolution string. Subclasses :class:`ValueError` so existing
    callers that already guard on ``ValueError`` keep working (#2648).
    """


#: Terminal resolutions a clearance gate may reach.
GATE_TERMINAL_RESOLUTIONS: frozenset[str] = frozenset({"cleared", "expired"})
#: Every resolution the ``signal.gate_projection`` chain vocabulary admits.
GATE_RESOLUTIONS: frozenset[str] = GATE_TERMINAL_RESOLUTIONS | {"pending"}


def validate_gate_resolution(resolution: str, *, allowed: frozenset[str] = GATE_RESOLUTIONS) -> str:
    """Return *resolution* when it is in *allowed*, else refuse.

    The check runs before any state mutation or signing so a rejected value
    never reaches the store or the HMAC chain.

    Args:
        resolution: The candidate resolution string.
        allowed: The admissible vocabulary for this boundary.

    Returns:
        The validated resolution.

    Raises:
        ClearanceResolutionRefusal: If *resolution* is outside *allowed*.
    """
    if resolution not in allowed:
        raise ClearanceResolutionRefusal(f"resolution must be one of {sorted(allowed)}, got {resolution!r}")
    return resolution


@dataclass(frozen=True)
class SignalGateProjectionDetails:
    """Structured payload for the ``signal.gate_projection`` event (#2556)."""

    blocker_content_hash: str
    clearance_task_id: str
    injected_edges: tuple[str, ...]
    graph_delta_hash: str
    scope_cell_id: str
    deadline: int
    resolution: str
    resolver: str
    last_state_hash: str
    journal_entry_hash: str
    blocker_entry_hash: str
    journal_prefix_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "blocker_content_hash": self.blocker_content_hash,
            "clearance_task_id": self.clearance_task_id,
            "injected_edges": list(self.injected_edges),
            "graph_delta_hash": self.graph_delta_hash,
            "scope_cell_id": self.scope_cell_id,
            "deadline": self.deadline,
            "resolution": self.resolution,
            "resolver": self.resolver,
            "last_state_hash": self.last_state_hash,
            "journal_entry_hash": self.journal_entry_hash,
            "blocker_entry_hash": self.blocker_entry_hash,
            "journal_prefix_hash": self.journal_prefix_hash,
        }


def record_signal_gate_projection(
    *,
    chain: AuditChainStore,
    blocker_content_hash: str,
    clearance_task_id: str,
    injected_edges: list[str],
    graph_delta_hash: str,
    scope_cell_id: str,
    deadline: int = 0,
    resolution: str = "pending",
    resolver: str = "",
    last_state_hash: str = "genesis",
    journal_entry_hash: str = "",
    blocker_entry_hash: str = "",
    journal_prefix_hash: str = "",
    actor: str = "clearance_gate",
) -> AuditEvent:
    """Append a ``signal.gate_projection`` event into *chain* (#2556).

    A typed ``blocker`` bulletin signal is not a chat line but a deterministic
    projection into the task graph: it materializes a clearance task plus
    injected ``depends_on`` edges onto the open dependent tasks in the blocker's
    scope, and the whole chain (blocker signal -> clearance task + injected edges
    -> resolution) is sealed as a receipt here. ``resolution`` is ``pending`` for
    the materialization entry and ``cleared`` / ``expired`` for a resolution
    entry; a resolution entry references the materialization entry hash via
    ``blocker_entry_hash``. ``graph_delta_hash`` is a pure function of the
    recorded detail fields, so a verifier recomputes it byte-identically from the
    chain entry alone -- strip the deterministic scheduler and this chain and the
    gate collapses to a logged blocker.

    Args:
        chain: The audit chain store accepting the entry.
        blocker_content_hash: ``sha256:`` digest over the blocker's stable
            content fields (agent, content, scope).
        clearance_task_id: The deterministic clearance-task id derived from the
            blocker content hash and the ordered journal prefix.
        injected_edges: The dependent task ids that received a ``depends_on``
            edge onto the clearance task (canonical sorted order).
        graph_delta_hash: The canonical task-graph delta hash the projection
            produced (64 hex chars); recomputable from the recorded fields.
        scope_cell_id: The blocker's cell scope the edges were injected into.
        deadline: Deterministic expiry deadline (Unix seconds; 0 = no expiry).
        resolution: ``pending`` (materialization) / ``cleared`` / ``expired``.
        resolver: Identity that resolved the clearance (empty for pending).
        last_state_hash: Prior gate-state anchor folded into this entry
            (``genesis`` for the first projection of a clearance task).
        journal_entry_hash: Lineage-spine entry hash the projection was sealed
            into; empty when no lineage sealer is wired.
        blocker_entry_hash: For a resolution entry, the HMAC of the
            materialization entry it clears; empty for the materialization entry.
        journal_prefix_hash: Digest of the ordered bulletin journal prefix the
            projection was computed against. Recorded so a replay after a
            restart reconstructs the same spec the in-process path held, and
            therefore seals the same lineage entry. Excluded from
            ``graph_delta_hash``, so recording it does not change any existing
            digest (#2648).
        actor: Recorded actor; defaults to ``"clearance_gate"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded in
        its details payload.

    Raises:
        ClearanceResolutionRefusal: If ``resolution`` is outside
            ``{pending, cleared, expired}``. The refusal happens before the
            payload is built, so an unrecognised resolution is never signed
            into the chain (#2648).
    """
    validate_gate_resolution(resolution)
    payload = SignalGateProjectionDetails(
        blocker_content_hash=blocker_content_hash,
        clearance_task_id=clearance_task_id,
        injected_edges=tuple(injected_edges),
        graph_delta_hash=graph_delta_hash,
        scope_cell_id=scope_cell_id,
        deadline=deadline,
        resolution=resolution,
        resolver=resolver,
        last_state_hash=last_state_hash,
        journal_entry_hash=journal_entry_hash,
        blocker_entry_hash=blocker_entry_hash,
        journal_prefix_hash=journal_prefix_hash,
    ).to_dict()
    return chain.log_with_prev_digest(
        event_type=EVENT_SIGNAL_GATE_PROJECTION,
        actor=actor or "clearance_gate",
        resource_type="signal_gate_projection",
        resource_id=clearance_task_id,
        details=payload,
    )


def record_escalation_receipt(
    *,
    chain: AuditChainStore,
    run_id: str,
    worker_id: str,
    stall_reason: str,
    recommended_action: str,
    journal_head_at_stall: str,
    window_size: int,
    fork_snapshot_sha: str,
    journal_entry_hash: str,
    actor: str = "escalation_receipt",
) -> AuditEvent:
    """Append an ``escalation.receipt`` event into *chain*.

    Mirrors a signed, spine-anchored stall escalation receipt into the
    HMAC-chained audit log so an operator can prove, from the chain alone, that
    an escalation was emitted for a stalled worker -- with which recommended
    action and which resume fork point -- without ever recording the journal
    payloads. The bound failure window itself lives in the run journal; this
    event records only its identity (``journal_head_at_stall`` + ``window_size``)
    and the receipt's spine anchor.

    Args:
        chain: The audit chain store accepting the entry.
        run_id: The run whose journal the receipt anchored.
        worker_id: The stalled worker the receipt covers.
        stall_reason: The structured stall reason recorded on the receipt.
        recommended_action: The deterministic recommended action.
        journal_head_at_stall: The run journal Merkle head at stall time.
        window_size: Number of journal entries bound into the failure window.
        fork_snapshot_sha: The resume fork point's snapshot sha, or empty when
            the receipt pinned no fork point.
        journal_entry_hash: The escalation-spine entry hash anchoring the
            receipt.
        actor: Recorded actor; defaults to ``"escalation_receipt"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded in
        its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_ESCALATION_RECEIPT,
        actor=actor,
        resource_type="escalation_receipt",
        resource_id=journal_entry_hash,
        details={
            "run_id": run_id,
            "worker_id": worker_id,
            "stall_reason": stall_reason,
            "recommended_action": recommended_action,
            "journal_head_at_stall": journal_head_at_stall,
            "window_size": window_size,
            "fork_snapshot_sha": fork_snapshot_sha,
            "journal_entry_hash": journal_entry_hash,
        },
    )


def record_sla_violation(
    *,
    chain: AuditChainStore,
    contract_id: str,
    contract_hash: str,
    subject_type: str,
    subject_id: str,
    tick_instant: int,
    breached_axes: list[str],
    requested_action: str,
    effective_action: str,
    remediation_blocked: bool,
    receipt_digest: str,
    actor: str = "sla_monitor",
) -> AuditEvent:
    """Append an ``sla.violation`` event into *chain* (#2549).

    Mirrors a signed, offline-verifiable SLA violation receipt (see
    :mod:`bernstein.core.orchestration.sla_receipt`) into the HMAC-chained audit
    log so an operator can prove, from the chain alone, that a per-goal contract
    was found breached at a named tick with a given remediation decision. The
    receipt itself embeds the evidence and re-derives its verdict offline; this
    event records only the receipt's identity and the decision.

    Args:
        chain: The audit chain store accepting the entry.
        contract_id: The breached contract's id (``sla_<hex>``).
        contract_hash: The content hash of the contract body.
        subject_type: ``schedule`` / ``task_family`` / ``envelope``.
        subject_id: The bound subject id.
        tick_instant: The supervisor tick instant the breach was detected at.
        breached_axes: The axis names that breached.
        requested_action: The first-choice remediation action.
        effective_action: The remediation action after the budget gate (the
            fallback when the requested action was blocked).
        remediation_blocked: True when the budget-envelope gate refused the
            requested spend-more remediation.
        receipt_digest: The violation receipt's payload digest.
        actor: Recorded actor; defaults to ``"sla_monitor"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded in
        its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_SLA_VIOLATION,
        actor=actor,
        resource_type="sla_contract",
        resource_id=contract_id,
        details={
            "contract_id": contract_id,
            "contract_hash": contract_hash,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "tick_instant": tick_instant,
            "breached_axes": breached_axes.copy(),
            "requested_action": requested_action,
            "effective_action": effective_action,
            "remediation_blocked": remediation_blocked,
            "receipt_digest": receipt_digest,
        },
    )


def record_admission_grant(
    *,
    chain: AuditChainStore,
    grant_id: str,
    pool: str,
    task_id: str,
    worker_id: str,
    ledger_head: str,
    over_limit: bool = False,
    actor: str = "admission",
) -> AuditEvent:
    """Mirror an admission grant into the HMAC chain (#2544).

    Records the grant's identity (its admission-ledger ``entry_hash``), the
    resource it holds, and the admission-ledger head at issue time. A verifier
    can prove from the audit chain alone that the grant existed at that head
    without carrying the admission ledger's payloads.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_ADMISSION_GRANT,
        actor=actor,
        resource_type="admission_pool",
        resource_id=pool,
        details={
            "grant_id": grant_id,
            "ledger_head": ledger_head,
            "over_limit": over_limit,
            "pool": pool,
            "task_id": task_id,
            "worker_id": worker_id,
        },
    )


def record_admission_waiver(
    *,
    chain: AuditChainStore,
    grant_id: str,
    resource: str,
    task_id: str,
    receipt_digest: str,
    ledger_head: str,
    actor: str = "admission",
) -> AuditEvent:
    """Mirror an ADVISE-posture waiver receipt into the HMAC chain (#2544).

    Every over-limit pass under ADVISE writes a signed waiver receipt; this
    event anchors the receipt's digest so soft enforcement degrades observably
    instead of silently.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_ADMISSION_WAIVER,
        actor=actor,
        resource_type="admission_waiver",
        resource_id=receipt_digest,
        details={
            "grant_id": grant_id,
            "ledger_head": ledger_head,
            "receipt_digest": receipt_digest,
            "resource": resource,
            "task_id": task_id,
        },
    )


def record_admission_quarantine(
    *,
    chain: AuditChainStore,
    entry_hash: str,
    target_kind: str,
    target: str,
    affected_count: int,
    ledger_head: str,
    actor: str = "admission",
) -> AuditEvent:
    """Mirror a class-freeze quarantine into the HMAC chain (#2544).

    Anchors the single admission-ledger quarantine row that carries the
    complete affected-set manifest, so an incident postmortem can prove the
    exact blast radius of a freeze offline.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_ADMISSION_QUARANTINE,
        actor=actor,
        resource_type="admission_quarantine",
        resource_id=entry_hash,
        details={
            "affected_count": affected_count,
            "entry_hash": entry_hash,
            "ledger_head": ledger_head,
            "target": target,
            "target_kind": target_kind,
        },
    )


def record_admission_tag_conformance(
    *,
    chain: AuditChainStore,
    task_id: str,
    worker_id: str,
    conformant: bool,
    receipt_digest: str,
    violation_count: int,
    actor: str = "admission",
) -> AuditEvent:
    """Mirror a post-hoc tag-conformance seal into the HMAC chain (#2544).

    A task that declared a tag contract (e.g. ``docs-only``) and broke it
    carries a signed violation receipt; this event anchors the seal so merge
    gates can prove the conformance verdict.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_ADMISSION_TAG_CONFORMANCE,
        actor=actor,
        resource_type="admission_tag_conformance",
        resource_id=receipt_digest,
        details={
            "conformant": conformant,
            "receipt_digest": receipt_digest,
            "task_id": task_id,
            "violation_count": violation_count,
            "worker_id": worker_id,
        },
    )


def record_intent_capsule(
    *,
    chain: AuditChainStore,
    task_id: str,
    plan_id: str,
    run_id: str,
    capsule_hash: str,
    goal_digest: str,
    allowed_action_classes_hash: str,
    expiry_ts: int,
    actor: str = "intent_capsule",
) -> AuditEvent:
    """Append an ``intent.capsule`` event into *chain* (#2514).

    Records the approved goal's intent capsule identity into the HMAC chain at
    approval time so every subsequent journal step is attributable to one
    approved capsule. Only hashes and identifiers are recorded -- never the goal
    text (bound by ``goal_digest``). A verifier recomputes the on-disk capsule's
    hash and checks it against ``capsule_hash`` here; a tampered capsule
    diverges.

    Args:
        chain: The audit chain store accepting the entry.
        task_id: The task the capsule governs.
        plan_id: The approved plan the capsule compiled from.
        run_id: The run whose journal the capsule is bound into.
        capsule_hash: ``sha256:`` content hash of the canonical capsule bytes.
        goal_digest: ``sha256:`` digest of the approved goal text.
        allowed_action_classes_hash: Compact commit to the capsule's allow-list.
        expiry_ts: Integer Unix timestamp after which the capsule is stale.
        actor: Recorded actor; defaults to ``"intent_capsule"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded in
        its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_INTENT_CAPSULE,
        actor=actor,
        resource_type="intent_capsule",
        resource_id=capsule_hash,
        details={
            "task_id": task_id,
            "plan_id": plan_id,
            "run_id": run_id,
            "capsule_hash": capsule_hash,
            "goal_digest": goal_digest,
            "allowed_action_classes_hash": allowed_action_classes_hash,
            "expiry_ts": expiry_ts,
        },
    )


def record_input_refusal(
    *,
    chain: AuditChainStore,
    boundary: str,
    json_path: str,
    schema_hash: str,
    value_digest: str,
    receipt_hash: str,
    resource_id: str,
    reason_code: str = "invalid",
    actor: str = "input_contract",
) -> AuditEvent:
    """Append an ``input.refusal_receipt`` event into *chain* (#2545).

    Anchors a signed input-refusal receipt into the HMAC chain so a malformed
    fire / claim / launch refused before any spawn is provable offline. Only
    the offending field's JSONPath, the declared schema hash, a digest of the
    rejected value (never the raw bytes), the boundary, the reason code, and the
    sealed receipt's content hash are recorded.

    Args:
        chain: The audit chain store accepting the entry.
        boundary: The input boundary that refused (e.g. ``schedule.fire``).
        json_path: JSONPath of the offending field (``$.params.<name>``).
        schema_hash: ``sha256:`` hash of the declared parameter schema.
        value_digest: ``sha256:`` digest of the rejected value (or ``""``).
        receipt_hash: ``sha256:`` content hash of the sealed refusal receipt.
        resource_id: The refused resource (schedule id, recipe name, task id).
        reason_code: Machine-stable reason (``bad_type``, ``missing_required``,
            ``unknown_param``, ``bad_choice``).
        actor: Recorded actor; defaults to ``"input_contract"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_INPUT_REFUSAL,
        actor=actor,
        resource_type="input_refusal",
        resource_id=receipt_hash,
        details={
            "boundary": boundary,
            "json_path": json_path,
            "schema_hash": schema_hash,
            "value_digest": value_digest,
            "receipt_hash": receipt_hash,
            "resource_id": resource_id,
            "reason_code": reason_code,
        },
    )


def record_context_capsule(
    *,
    chain: AuditChainStore,
    task_id: str,
    run_id: str,
    params_hash: str,
    capsule_hash: str,
    audit_chain_head: str,
    intent_capsule_hash: str = "",
    actor: str = "context_capsule",
) -> AuditEvent:
    """Append a ``context.capsule`` event into *chain* (#2545).

    Mirrors a spawned worker's runtime context capsule identity into the HMAC
    chain so a verifier holding only the journal and the chain can recompute the
    capsule byte-identically at the recorded chain position. Only hashes and
    identifiers are recorded.

    Args:
        chain: The audit chain store accepting the entry.
        task_id: The task the capsule was built for.
        run_id: The run whose journal the capsule hash is bound into.
        params_hash: ``sha256:`` hash of the validated parameter map (same value
            the spawn record and journal carry).
        capsule_hash: ``sha256:`` content hash of the canonical capsule bytes.
        audit_chain_head: The chain head the capsule pinned at spawn.
        intent_capsule_hash: The #2514 intent capsule hash when one exists.
        actor: Recorded actor; defaults to ``"context_capsule"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_CONTEXT_CAPSULE,
        actor=actor,
        resource_type="context_capsule",
        resource_id=capsule_hash,
        details={
            "task_id": task_id,
            "run_id": run_id,
            "params_hash": params_hash,
            "capsule_hash": capsule_hash,
            "audit_chain_head": audit_chain_head,
            "intent_capsule_hash": intent_capsule_hash,
        },
    )


def record_intent_drift(
    *,
    chain: AuditChainStore,
    task_id: str,
    capsule_hash: str,
    verdict_hash: str,
    divergent_count: int,
    escalation_journal_entry_hash: str,
    actor: str = "intent_drift",
) -> AuditEvent:
    """Append an ``intent.drift`` event into *chain* (#2514).

    Mirrors a signed drift escalation's identity into the HMAC chain so an
    operator can prove, from the chain alone, that a drift escalation was
    emitted against a named capsule with a given deterministic verdict. The
    divergent events live in the run journal and the escalation receipt; this
    event records only their identity.

    Args:
        chain: The audit chain store accepting the entry.
        task_id: The task whose run drifted.
        capsule_hash: ``sha256:`` content hash of the violated capsule.
        verdict_hash: The deterministic conformance verdict hash.
        divergent_count: Number of divergent journal steps.
        escalation_journal_entry_hash: The escalation-spine anchor of the signed
            drift receipt.
        actor: Recorded actor; defaults to ``"intent_drift"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded in
        its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_INTENT_DRIFT,
        actor=actor,
        resource_type="intent_drift",
        resource_id=capsule_hash,
        details={
            "task_id": task_id,
            "capsule_hash": capsule_hash,
            "verdict_hash": verdict_hash,
            "divergent_count": divergent_count,
            "escalation_journal_entry_hash": escalation_journal_entry_hash,
        },
    )


def record_intent_journal_seal(
    *,
    chain: AuditChainStore,
    task_id: str,
    run_id: str,
    capsule_hash: str,
    journal_head: str,
    event_count: int,
    actor: str = "intent_capsule",
) -> AuditEvent:
    """Append an ``intent.journal_seal`` event into *chain* (#2649).

    Commits the run journal's END to signed state. The journal's Merkle chain
    recomputes from genesis using positional indices, so every prefix of a valid
    journal is itself a valid journal -- a worker can drop the trailing rows
    that convict it and the remaining history verifies cleanly. Recording the
    head hash and the event count gives the verifier an independent commitment
    to compare against, which is the only way truncation becomes detectable.

    Args:
        chain: The audit chain store accepting the entry.
        task_id: The task whose capsule governs the run.
        run_id: The run whose journal is sealed.
        capsule_hash: ``sha256:`` hash of the capsule governing the run.
        journal_head: The journal's final ``event_hash`` (its Merkle head).
        event_count: The number of events the journal contained when sealed.
        actor: Recorded actor; defaults to ``"intent_capsule"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded in
        its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_INTENT_JOURNAL_SEAL,
        actor=actor,
        resource_type="intent_capsule",
        resource_id=capsule_hash,
        details={
            "task_id": task_id,
            "run_id": run_id,
            "capsule_hash": capsule_hash,
            "journal_head": journal_head,
            "event_count": int(event_count),
        },
    )


def record_webhook_node_receipt(
    *,
    chain: AuditChainStore,
    direction: str,
    event_id: str,
    source: str,
    event_hash: str = "",
    journal_root: str = "",
    result_hash: str = "",
    journal_head: str = "",
    journal_entry_hash: str,
    actor: str = "webhook_node",
) -> AuditEvent:
    """Append a ``webhook_node.receipt`` event into *chain* (#2310).

    Mirrors one webhook-node receipt into the HMAC-chained audit log so an
    operator can prove, from the chain alone, that a no-code flow step ran under
    a signed inbound event and returned a signed outbound result. Only hashes
    and the source label are recorded -- never the webhook body.

    Args:
        chain: The audit chain store accepting the entry.
        direction: ``inbound`` or ``outbound``.
        event_id: The Standard Webhooks message id the receipt covers.
        source: The calling bus / builder label the inbound event came from.
        event_hash: Content hash of the inbound event (inbound receipts).
        journal_root: The spawned run's journal root the inbound event anchors
            to (inbound receipts).
        result_hash: Content hash of the returned result (outbound receipts).
        journal_head: The run journal head the outbound result binds to
            (outbound receipts).
        journal_entry_hash: The webhook-node spine entry hash anchoring the
            receipt; a verifier holding the spine can recompute it.
        actor: Recorded actor; defaults to ``"webhook_node"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded in
        its details payload.
    """
    details: dict[str, Any] = {
        "direction": direction,
        "event_id": event_id,
        "source": source,
        "journal_entry_hash": journal_entry_hash,
    }
    if event_hash:
        details["event_hash"] = event_hash
    if journal_root:
        details["journal_root"] = journal_root
    if result_hash:
        details["result_hash"] = result_hash
    if journal_head:
        details["journal_head"] = journal_head
    return chain.log_with_prev_digest(
        event_type=EVENT_WEBHOOK_NODE_RECEIPT,
        actor=actor,
        resource_type="webhook_node_receipt",
        resource_id=event_id,
        details=details,
    )


def record_trigger_receipt(
    *,
    chain: AuditChainStore,
    trigger_id: str,
    platform: str,
    request_path: str,
    payload_digest: str,
    graph_digest: str,
    scope: str,
    outcome: str,
    receipt_digest: str,
    refusal_reason: str = "",
    suppressed_refusals: int = 0,
    actor: str = "automation_bridge",
) -> AuditEvent:
    """Append a ``trigger.receipt.*`` event into *chain* (#2512).

    Anchors one inbound automation trigger in the HMAC chain. The event type is
    :data:`EVENT_TRIGGER_RECEIPT_ISSUED` for an admitted trigger and
    :data:`EVENT_TRIGGER_RECEIPT_REFUSED` for one turned away, so a refusal is
    as discoverable as an admission. ``receipt_digest`` is the hash of the
    signed receipt binding: a verifier holding the platform's stored copy
    recomputes it and matches this row.

    Args:
        chain: The audit chain store accepting the entry.
        trigger_id: The caller-supplied trigger id (the replay nonce).
        platform: The automation platform label the trigger arrived from.
        request_path: The request path the trigger was fired at.
        payload_digest: Content hash of the raw trigger body.
        graph_digest: Digest of the canonical task graph the payload projects;
            empty for a refusal, which projects nothing.
        scope: The scope granted to the admitted trigger.
        outcome: ``admitted`` or ``refused``.
        receipt_digest: Hash of the signed receipt binding.
        refusal_reason: Why the trigger was refused; empty when admitted.
        suppressed_refusals: Refusals turned away since the last anchored one
            without an individual entry, because the refusal budget was
            exhausted. Recorded so the chain never hides that they happened.
        actor: Recorded actor; defaults to ``"automation_bridge"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded in
        its details payload.
    """
    details: dict[str, Any] = {
        "trigger_id": trigger_id,
        "platform": platform,
        "request_path": request_path,
        "payload_digest": payload_digest,
        "scope": scope,
        "outcome": outcome,
        "receipt_digest": receipt_digest,
    }
    if graph_digest:
        details["graph_digest"] = graph_digest
    if refusal_reason:
        details["refusal_reason"] = refusal_reason
    if suppressed_refusals:
        details["suppressed_refusals"] = suppressed_refusals
    event_type = EVENT_TRIGGER_RECEIPT_ISSUED if outcome == "admitted" else EVENT_TRIGGER_RECEIPT_REFUSED
    return chain.log_with_prev_digest(
        event_type=event_type,
        actor=actor,
        resource_type="automation_trigger",
        resource_id=trigger_id,
        details=details,
    )


def record_status_proof(
    *,
    chain: AuditChainStore,
    event_id: str,
    run_id: str,
    status: str,
    producing_event_digest: str,
    proof_digest: str,
    actor: str = "automation_bridge",
) -> AuditEvent:
    """Append a ``status.proof.emitted`` event into *chain* (#2512).

    Records the status the bridge reported outward together with the digest of
    the notification event that produced it. A verifier presented a delivered
    callback envelope re-derives ``producing_event_digest`` from the carried
    payload and compares ``status`` against this row, so a status altered on the
    wire is detected and the chain's recorded value is recoverable.

    Args:
        chain: The audit chain store accepting the entry.
        event_id: The notification event id the callback carries.
        run_id: The run the status belongs to.
        status: The reported status.
        producing_event_digest: Content hash of the canonical notification
            payload that produced the status.
        proof_digest: Hash of the signed proof binding.
        actor: Recorded actor; defaults to ``"automation_bridge"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded in
        its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_STATUS_PROOF_EMITTED,
        actor=actor,
        resource_type="automation_status",
        resource_id=event_id,
        details={
            "event_id": event_id,
            "run_id": run_id,
            "status": status,
            "producing_event_digest": producing_event_digest,
            "proof_digest": proof_digest,
        },
    )


def record_odata_writeback(
    *,
    chain: AuditChainStore,
    connection_name: str,
    entity_set: str,
    entity_key: str,
    etag_observed: str,
    payload_content_hash: str,
    http_status: int,
    draft_flow: bool = False,
    activate_action: str = "",
    actor: str = "odata_writeback",
) -> AuditEvent:
    """Append an ``odata.writeback_receipt`` event into *chain* (#2886).

    Anchors one OData system-of-record write-back in the HMAC chain. The event
    binds the concurrency token the PATCH was gated on (``etag_observed``) and
    the content hash of the sent payload, so an auditor holding the sent body
    re-hashes it and matches ``payload_content_hash`` on this row, and confirms
    from the chain alone -- via ``bernstein audit verify`` with no new verb --
    that the change targeted the recorded entity against that specific ETag. A
    mutated row breaks the HMAC chain at its exact position.

    Args:
        chain: The audit chain store accepting the entry.
        connection_name: Stable connection label.
        entity_set: The OData entity set written to.
        entity_key: Canonical key predicate inner text (e.g. ``id=1``).
        etag_observed: The ``If-Match`` ETag the PATCH was gated on; empty only
            for a draft-flow create whose activation carries no prior ETag.
        payload_content_hash: ``sha256:`` digest of the canonical sent payload.
        http_status: The HTTP status the write returned.
        draft_flow: Whether the write went through a draft-activate flow.
        activate_action: The bound activate action name (draft flow only).
        actor: Recorded actor; defaults to ``"odata_writeback"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded in
        its details payload.
    """
    details: dict[str, Any] = {
        "connection": connection_name,
        "entity_set": entity_set,
        "entity_key": entity_key,
        "etag_observed": etag_observed,
        "payload_content_hash": payload_content_hash,
        "http_status": http_status,
        "draft_flow": draft_flow,
    }
    if activate_action:
        details["activate_action"] = activate_action
    return chain.log_with_prev_digest(
        event_type=EVENT_ODATA_WRITEBACK,
        actor=actor,
        resource_type="odata_entity",
        resource_id=f"{entity_set}({entity_key})",
        details=details,
    )


def record_governance_decision(
    *,
    chain: AuditChainStore,
    subject: str,
    action: str,
    verdict: str,
    inputs_hash: str,
    journal_entry_hash: str,
    run_id: str,
    actor: str = "governance",
) -> AuditEvent:
    """Append a ``governance.decision`` event into *chain*.

    Mirrors one signed, spine-anchored governance decision into the HMAC-chained
    audit log so an operator can prove, from the chain alone, that an access or
    budget decision bound the claimed inputs to a named spine entry. A denied
    access carries a ``deny`` verdict and a budget breach a ``refuse`` verdict --
    both are signed records, not merely logged. Only hashes, the verdict, and the
    anchor are recorded.

    Args:
        chain: The audit chain store accepting the entry.
        subject: The subject the decision is about (a seat / actor / user id).
        action: The requested action (a permission string, or ``budget``).
        verdict: One of ``allow`` / ``deny`` / ``refuse``.
        inputs_hash: ``sha256:`` hash of the decision's projection inputs.
        journal_entry_hash: The lineage-spine entry hash anchoring the decision
            record; a verifier holding the spine can recompute it.
        run_id: The run whose spine the decision anchors to.
        actor: Recorded actor; defaults to ``"governance"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded in
        its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_GOVERNANCE_DECISION,
        actor=actor,
        resource_type="governance_decision",
        resource_id=subject,
        details={
            "subject": subject,
            "action": action,
            "verdict": verdict,
            "inputs_hash": inputs_hash,
            "journal_entry_hash": journal_entry_hash,
            "run_id": run_id,
        },
    )


def record_a2a_message_receipt(
    *,
    chain: AuditChainStore,
    direction: str,
    task_uuid: str,
    state: str,
    reason_code: str,
    message_hash: str,
    peer_card_fingerprint: str,
    journal_entry_hash: str,
    actor: str = "a2a_message",
) -> AuditEvent:
    """Append an ``a2a.message_receipt`` event into *chain* (#2304).

    Mirrors one A2A message receipt into the HMAC-chained audit log so a
    reviewer can prove, from the chain alone, that a cross-agent message
    happened with the exact inputs claimed, without trusting either agent's
    logs. Only hashes, the peer fingerprint, the task uuid, and the lifecycle
    state are recorded -- never the message body.

    Args:
        chain: The audit chain store accepting the entry.
        direction: ``inbound`` or ``outbound``.
        task_uuid: The A2A task uuid the message belongs to (the trace root).
        state: The A2A v1.0 task state the message carried.
        reason_code: The reason code the state maps to on the journal.
        message_hash: Content hash of the message.
        peer_card_fingerprint: ``sha256:`` fingerprint of the peer's card key.
        journal_entry_hash: The message-receipt spine entry hash anchoring the
            receipt; a verifier holding the spine can recompute it.
        actor: Recorded actor; defaults to ``"a2a_message"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded in
        its details payload.
    """
    details: dict[str, Any] = {
        "direction": direction,
        "task_uuid": task_uuid,
        "state": state,
        "reason_code": reason_code,
        "message_hash": message_hash,
        "peer_card_fingerprint": peer_card_fingerprint,
        "journal_entry_hash": journal_entry_hash,
    }
    return chain.log_with_prev_digest(
        event_type=EVENT_A2A_MESSAGE_RECEIPT,
        actor=actor,
        resource_type="a2a_message_receipt",
        resource_id=task_uuid,
        details=details,
    )


def record_activity_result(
    *,
    chain: AuditChainStore,
    run_id: str,
    stage_id: str,
    kind: str,
    artifact_hash: str,
    evidence_set_hash: str,
    terminal_state: str,
    reason_code: str,
    journal_index: int,
    journal_event_hash: str,
    actor: str = "activity",
) -> AuditEvent:
    """Append an ``activity.result`` event into *chain* (#2311).

    Mirrors one typed activity boundary crossing into the HMAC-chained audit log
    so an operator can prove, from the chain alone, that a modality-agnostic
    activity (research, browser/computer-use, data, ops, coding) ran under the
    deterministic scheduler with a given evidence set. The ``evidence_set_hash``
    is a pure function of the observations the activity gathered, so it is
    replay-invariant; ``artifact_hash`` fixes the (stochastic) result the
    activity produced, and ``journal_index`` / ``journal_event_hash`` tie both to
    the exact run-journal entry the activity was anchored at. Only hashes, the
    kind, the terminal state, and the reason code are recorded -- never the
    artifact body or the fetched evidence.

    Args:
        chain: The audit chain store accepting the entry.
        run_id: The run whose journal the activity was anchored into.
        stage_id: The scheduler stage id that crossed the boundary.
        kind: The agent modality (``research`` / ``browser`` / ``data`` /
            ``ops`` / ``coding``).
        artifact_hash: SHA-256 of the canonical activity artifact.
        evidence_set_hash: SHA-256 over the observation set (replay-invariant).
        terminal_state: The typed terminal state
            (``completed`` / ``refused`` / ``failed`` / ``timed_out``).
        reason_code: The machine reason code for the terminal state.
        journal_index: 0-based journal index of the anchoring entry.
        journal_event_hash: The anchoring journal entry's Merkle ``event_hash``.
        actor: Recorded actor; defaults to ``"activity"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded in
        its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_ACTIVITY_RESULT,
        actor=actor,
        resource_type="activity_result",
        resource_id=stage_id,
        details={
            "run_id": run_id,
            "stage_id": stage_id,
            "kind": kind,
            "artifact_hash": artifact_hash,
            "evidence_set_hash": evidence_set_hash,
            "terminal_state": terminal_state,
            "reason_code": reason_code,
            "journal_index": journal_index,
            "journal_event_hash": journal_event_hash,
        },
    )


def record_evidence_bundle(
    *,
    chain: AuditChainStore,
    task_id: str,
    bundle_hash: str,
    item_count: int,
    gate_passed: bool,
    journal_entry_hash: str,
    actor: str = "evidence_bundle",
) -> AuditEvent:
    """Append an ``evidence.bundle`` event into *chain* (#2362).

    Mirrors a signed, spine-anchored verification evidence bundle into the
    HMAC-chained audit log so an operator can prove, from the chain alone, that a
    bundle of proof-of-done evidence was sealed for a task. Only the bundle hash,
    the item count, the gate verdict, and the spine anchor are recorded -- never
    the evidence bytes (the test-runner output, coverage report, or screenshot).
    A verifier holding the stored blobs can recompute the bundle byte-identically
    and confirm the evidence is chain-attested.

    Args:
        chain: The audit chain store accepting the entry.
        task_id: The task the bundle was sealed for.
        bundle_hash: ``sha256:`` hash of the canonical bundle binding bytes.
        item_count: Number of evidence items bound into the bundle.
        gate_passed: Whether every required producer passed (advisory failures
            never block).
        journal_entry_hash: The evidence-spine entry hash anchoring the bundle.
        actor: Recorded actor; defaults to ``"evidence_bundle"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded in
        its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_EVIDENCE_BUNDLE,
        actor=actor,
        resource_type="evidence_bundle",
        resource_id=task_id,
        details={
            "task_id": task_id,
            "bundle_hash": bundle_hash,
            "item_count": item_count,
            "gate_passed": gate_passed,
            "journal_entry_hash": journal_entry_hash,
        },
    )


def record_provider_state_mutation(
    *,
    chain: AuditChainStore,
    run_id: str,
    agent_id: str,
    mutation_kind: str,
    content_address: str,
    step_index: int,
    flagged: bool,
    journal_head: str,
    actor: str = "provider_state",
) -> AuditEvent:
    """Append a ``provider.state_mutation`` event into *chain* (#2507).

    Mirrors an observed provider-side context mutation's identity into the
    HMAC-chained audit log after the mutation was chained into the run's
    replay journal. Only the mutation kind, its content address, the step
    index, the deterministic-mode flag, and the journal head anchor are
    recorded -- never the mutated context. A verifier holding the run journal
    can confirm the mutation entry is chain-attested and that a flagged
    mutation fails replay verification closed.

    Args:
        chain: The audit chain store accepting the entry.
        run_id: The run whose journal recorded the mutation.
        agent_id: The agent session the mutation was observed for.
        mutation_kind: Provider-reported mutation kind (for example
            ``compact_boundary``).
        content_address: ``H(kind, before_digest, after_digest, step_index)``
            of the chained journal entry.
        step_index: 0-based position within the session's signal order.
        flagged: Whether the mutation arrived in deterministic mode.
        journal_head: The run journal head after the entry was chained.
        actor: Recorded actor; defaults to ``"provider_state"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded
        in its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_PROVIDER_STATE_MUTATION,
        actor=actor,
        resource_type="provider_state_mutation",
        resource_id=content_address,
        details={
            "run_id": run_id,
            "agent_id": agent_id,
            "mutation_kind": mutation_kind,
            "content_address": content_address,
            "step_index": step_index,
            "flagged": flagged,
            "journal_head": journal_head,
        },
    )


def record_work_ledger_anchor(
    *,
    chain: AuditChainStore,
    run_id: str,
    head_hash: str,
    entry_count: int,
    chunk_count: int,
    ref: str,
    tree_sha: str,
    actor: str = "work_ledger",
) -> AuditEvent:
    """Append a ``work_ledger.anchor`` event into *chain* (#2358).

    Mirrors a work-ledger anchor point into the HMAC-chained audit log so an
    operator can prove, from the chain alone, that a run's resumable task-graph
    state was anchored at a specific head. Only the run id, the chain head, the
    entry/chunk counts, the ref name, and the deterministic tree sha are
    recorded -- never the transition payloads. A verifier holding the repository
    walks the anchored chain, recomputes the head, and confirms the resume
    point is chain-attested.

    Args:
        chain: The audit chain store accepting the entry.
        run_id: The run whose ledger was anchored.
        head_hash: Head entry hash of the anchored chain.
        entry_count: Number of ledger entries anchored.
        chunk_count: Number of chunk blobs in the anchor tree.
        ref: The fully-qualified ledger ref that was updated.
        tree_sha: Deterministic tree sha of the anchor (its identity).
        actor: Recorded actor; defaults to ``"work_ledger"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded in
        its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_WORK_LEDGER_ANCHOR,
        actor=actor,
        resource_type="work_ledger",
        resource_id=run_id,
        details={
            "run_id": run_id,
            "head_hash": head_hash,
            "entry_count": entry_count,
            "chunk_count": chunk_count,
            "ref": ref,
            "tree_sha": tree_sha,
        },
    )


def record_checkpoint_retry(
    *,
    chain: AuditChainStore,
    task_id: str,
    retry_mode: str,
    requested_mode: str,
    capability: str,
    checkpoint_event_hash: str,
    checkpoint_journal_index: int,
    workspace_match: bool,
    downgrade_reason: str,
    decision_hash: str,
    journal_event_hash: str,
    journal_entry_hash: str,
    actor: str = "checkpoint_retry",
) -> AuditEvent:
    """Append a ``retry.checkpoint_decision`` event into *chain*.

    Mirrors a checkpointed-retry decision (issue #2359) into the HMAC-chained
    audit log so replay can distinguish warm from cold retries and the retry
    lineage is inspectable from the chain alone. Only identifiers, hashes, the
    mode, and the downgrade reason are recorded -- never prompt content, gate
    output, or provider session state.

    Args:
        chain: The audit chain store accepting the entry.
        task_id: The failed task whose retry was decided.
        retry_mode: Effective mode (``warm`` / ``fork`` / ``cold``).
        requested_mode: The mode the retry policy asked for.
        capability: The adapter's declared checkpointed-retry capability
            (``resume`` / ``fork`` / ``none``).
        checkpoint_event_hash: Merkle hash of the checkpoint row in the
            task's event journal (empty when no checkpoint existed).
        checkpoint_journal_index: 0-based journal index of that row, or -1.
        workspace_match: Whether the recorded workspace hash matched the
            live worktree at decision time.
        downgrade_reason: Why a non-cold request became cold (or a fork
            became warm); empty when the request was honored.
        decision_hash: SHA-256 of the canonical decision projection.
        journal_event_hash: Merkle hash of the decision row appended to the
            task's event journal.
        journal_entry_hash: Lineage-spine entry hash anchoring the decision
            artifact (empty when lineage recording is disabled).
        actor: Recorded actor; defaults to ``"checkpoint_retry"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded
        in its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_CHECKPOINT_RETRY,
        actor=actor,
        resource_type="checkpoint_retry",
        resource_id=task_id,
        details={
            "task_id": task_id,
            "retry_mode": retry_mode,
            "requested_mode": requested_mode,
            "capability": capability,
            "checkpoint_event_hash": checkpoint_event_hash,
            "checkpoint_journal_index": checkpoint_journal_index,
            "workspace_match": workspace_match,
            "downgrade_reason": downgrade_reason,
            "decision_hash": decision_hash,
            "journal_event_hash": journal_event_hash,
            "journal_entry_hash": journal_entry_hash,
        },
    )


def record_routing_failover_receipt(
    *,
    chain: AuditChainStore,
    role: str,
    task_id: str,
    decision_hash: str,
    chosen_index: int,
    reason: str,
    chain_considered: list[dict[str, Any]],
    probe_results: list[dict[str, Any]],
    kind: str = "dispatch",
    actor: str = "provider_availability",
) -> AuditEvent:
    """Append a ``routing.failover_receipt`` event into *chain* (#2355).

    Mirrors one provider-availability routing decision into the HMAC-chained
    audit log: the per-role fallback chain that was considered, the recorded
    probe outcomes it was evaluated against, the chosen chain position, the
    reason the decision fired, and the deterministic decision hash. The
    decision is a pure function of the chain and the recorded probe set (see
    :func:`bernstein.core.routing.provider_availability.decide_route`), so a
    verifier holding this receipt recomputes the decision byte-identically
    and confirms the dispatched provider was the deterministic choice -- not
    an operator override and not a race.

    Args:
        chain: The audit chain store accepting the entry.
        role: The role whose fallback chain was evaluated.
        task_id: The task being dispatched (empty for drills).
        decision_hash: ``sha256:`` hash of the canonical decision projection.
        chosen_index: Selected chain position (``-1`` when no element was
            healthy and the dispatch was refused).
        reason: Why the decision fired (``primary_healthy`` | ``failover`` |
            ``no_healthy_provider``).
        chain_considered: The declared chain as recorded dicts
            (``adapter``/``model``/``conformance`` per element).
        probe_results: Recorded probe outcomes aligned with the chain.
        kind: ``"dispatch"`` for spawn-time decisions, ``"drill"`` for
            ``bernstein doctor --failover-drill`` simulations.
        actor: Recorded actor; defaults to ``"provider_availability"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded
        in its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_ROUTING_FAILOVER_RECEIPT,
        actor=actor,
        resource_type="routing_receipt",
        resource_id=task_id or role,
        details={
            "role": role,
            "task_id": task_id,
            "decision_hash": decision_hash,
            "chosen_index": chosen_index,
            "reason": reason,
            "chain_considered": chain_considered,
            "probe_results": probe_results,
            "kind": kind,
        },
    )


def record_endpoint_certification(
    *,
    chain: AuditChainStore,
    fingerprint: str,
    model: str,
    engine: str,
    suite_version: int,
    transcript_hash: str,
    certified_roles: list[str],
    rejected_roles: list[str],
    journal_entry_hash: str,
    actor: str = "endpoint_certification",
) -> AuditEvent:
    """Append an ``endpoint.certification`` event into *chain* (#2356).

    Mirrors a sealed endpoint certification receipt into the HMAC-chained
    audit log so an operator can prove, from the chain alone, that a given
    OpenAI-compatible endpoint was certified (or rejected) for a set of
    roles by a specific conformance suite version. Only the endpoint
    fingerprint, the model/engine labels, the transcript hash, the role
    verdict summary, and the spine anchor are recorded -- never the
    endpoint's base URL credentials or its response bodies.

    Args:
        chain: The audit chain store accepting the entry.
        fingerprint: Stable hex fingerprint of the ``(base_url, model)`` pair.
        model: Model id the conformance suite ran against.
        engine: Operator-supplied runtime label (may be empty).
        suite_version: Conformance suite version that produced the verdicts.
        transcript_hash: ``sha256:`` hash of the canonical probe transcript.
        certified_roles: Sorted roles the receipt certifies.
        rejected_roles: Sorted roles the receipt rejects.
        journal_entry_hash: The certification spine entry hash anchoring the
            receipt.
        actor: Recorded actor; defaults to ``"endpoint_certification"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded
        in its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_ENDPOINT_CERTIFICATION,
        actor=actor,
        resource_type="endpoint_certification",
        resource_id=fingerprint,
        details={
            "fingerprint": fingerprint,
            "model": model,
            "engine": engine,
            "suite_version": suite_version,
            "transcript_hash": transcript_hash,
            "certified_roles": certified_roles,
            "rejected_roles": rejected_roles,
            "journal_entry_hash": journal_entry_hash,
        },
    )


def record_task_mailbox_message(
    *,
    chain: AuditChainStore,
    task_id: str,
    seq: int,
    kind: str,
    sender: str,
    sender_card_fingerprint: str,
    body_hash: str,
    entry_hash: str,
    redaction_count: int,
    actor: str = "task_mailbox",
) -> AuditEvent:
    """Append a ``task.mailbox_message`` event into *chain* (#2357).

    Mirrors one accepted worker mailbox message into the HMAC-chained audit
    log so a reviewer can prove, from the chain alone, that a cross-worker
    message was delivered with the exact payload hash and chain position
    claimed. Only hashes, the kind, the sender attribution, and the mailbox
    chain link are recorded -- never the message body.

    Args:
        chain: The audit chain store accepting the entry.
        task_id: The recipient task.
        seq: The message's global position in the mailbox journal.
        kind: The typed message kind (``finding`` / ``artefact_ref`` /
            ``question``).
        sender: The posting worker's identifier.
        sender_card_fingerprint: ``sha256:`` fingerprint of the sender's
            agent card key, or ``"unregistered"``.
        body_hash: ``sha256:`` hash of the stored (post-redaction) body.
        entry_hash: The mailbox journal's ``hmac-sha256:`` chain tag for
            this entry; the cross-check anchor.
        redaction_count: DLP redactions applied on the write path.
        actor: Recorded actor; defaults to ``"task_mailbox"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded
        in its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_TASK_MAILBOX_MESSAGE,
        actor=actor,
        resource_type="task_mailbox_message",
        resource_id=task_id,
        details={
            "task_id": task_id,
            "seq": seq,
            "kind": kind,
            "sender": sender,
            "sender_card_fingerprint": sender_card_fingerprint,
            "body_hash": body_hash,
            "entry_hash": entry_hash,
            "redaction_count": redaction_count,
        },
    )


def record_task_claim_receipt(
    *,
    chain: AuditChainStore,
    task_id: str,
    role: str,
    claimed_by: str,
    depends_on: list[str],
    task_version: int,
    claim_path: str,
    actor: str = "task_server",
) -> AuditEvent:
    """Append a ``task.claim_receipt`` event into *chain* (#2357).

    Records the dependency snapshot a claim was granted under, making every
    claim a journal entry on the audit chain. Together with the dependency
    gate (the claim API never offers a task whose ``depends_on`` are not
    terminal), the chain lets an operator reconstruct claim eligibility
    offline and prove no worker received a gated task.

    Args:
        chain: The audit chain store accepting the entry.
        task_id: The claimed task.
        role: The task's role lane.
        claimed_by: The claiming session or agent identifier (may be empty).
        depends_on: The task's declared dependency ids at claim time.
        task_version: The task version after the claim transition.
        claim_path: Which endpoint granted the claim
            (``next`` / ``by_id`` / ``batch``).
        actor: Recorded actor; defaults to ``"task_server"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded
        in its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_TASK_CLAIM_RECEIPT,
        actor=actor,
        resource_type="task_claim",
        resource_id=task_id,
        details={
            "task_id": task_id,
            "role": role,
            "claimed_by": claimed_by,
            "depends_on": depends_on.copy(),
            "task_version": task_version,
            "claim_path": claim_path,
        },
    )


def record_claim_journal_receipt(
    *,
    chain: AuditChainStore,
    kind: str,
    tracker: str,
    ticket_id: str,
    role: str,
    claimer_id: str,
    node_id: str,
    lease_expires_at: float,
    prev_entry_hash: str,
    journal_entry_hash: str,
    supersedes: str | None = None,
    winner_claimer_id: str | None = None,
    winner_entry_hash: str | None = None,
    superseded_node_id: str | None = None,
    superseded_claimer_id: str | None = None,
    actor: str = "claim_journal",
) -> AuditEvent:
    """Append a ``cluster.claim_journal_receipt`` event into *chain* (#2558).

    Mirrors one leaderless-MESH claim-journal receipt into the HMAC-chained
    audit log so an operator can prove, from the chain alone, that a self-claim
    (or its release / renewal / expiry / supersession) was recorded with the
    exact identity, lease, and chain position claimed -- without trusting any
    single node. ``journal_entry_hash`` is the receipt's Merkle ``entry_hash``;
    a verifier holding the journal recomputes it byte-identically and confirms
    the anchor. For a ``supersede`` receipt the ``winner_*`` fields name the
    concurrent claim that won by the deterministic lowest-``entry_hash`` rule.
    Only identifiers and hashes are recorded -- never ticket or workspace
    contents.

    Args:
        chain: The audit chain store accepting the entry.
        kind: The receipt kind (``claim`` / ``release`` / ``renew`` /
            ``expire`` / ``supersede``).
        tracker: Tracker adapter name the claim is scoped to.
        ticket_id: Tracker-side ticket id.
        role: Bernstein role lane the claim covers.
        claimer_id: The receipt's own claimer identity -- who the receipt is
            *from*. For a ``supersede`` receipt this is the reconciling node
            that emitted and signed it, not the loser (the loser is carried in
            ``superseded_claimer_id`` as referenced data).
        node_id: The node install identity the receipt is *from* -- the node
            whose Ed25519 key signs it. For a ``supersede`` receipt this is the
            reconciling node, not the superseded claim's node.
        lease_expires_at: Unix timestamp the lease expires (``0`` when none).
        prev_entry_hash: The journal head the receipt chained onto.
        journal_entry_hash: The receipt's own Merkle ``entry_hash`` (the anchor).
        supersedes: For a ``supersede`` receipt, the losing claim's
            ``entry_hash``; ``None`` otherwise.
        winner_claimer_id: For a ``supersede`` receipt, the winning claimer.
        winner_entry_hash: For a ``supersede`` receipt, the winning claim's
            ``entry_hash``.
        superseded_node_id: For a ``supersede`` receipt, the losing claim's
            node identity, carried as referenced data (what the receipt is
            *about*); ``None`` otherwise.
        superseded_claimer_id: For a ``supersede`` receipt, the losing claim's
            claimer identity, carried as referenced data; ``None`` otherwise.
        actor: Recorded actor; defaults to ``"claim_journal"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded
        in its details payload.
    """
    details: dict[str, Any] = {
        "kind": kind,
        "tracker": tracker,
        "ticket_id": ticket_id,
        "role": role,
        "claimer_id": claimer_id,
        "node_id": node_id,
        "lease_expires_at": lease_expires_at,
        "prev_entry_hash": prev_entry_hash,
        "journal_entry_hash": journal_entry_hash,
    }
    if supersedes is not None:
        details["supersedes"] = supersedes
    if winner_claimer_id is not None:
        details["winner_claimer_id"] = winner_claimer_id
    if winner_entry_hash is not None:
        details["winner_entry_hash"] = winner_entry_hash
    if superseded_node_id is not None:
        details["superseded_node_id"] = superseded_node_id
    if superseded_claimer_id is not None:
        details["superseded_claimer_id"] = superseded_claimer_id
    return chain.log_with_prev_digest(
        event_type=EVENT_CLAIM_JOURNAL_RECEIPT,
        actor=actor,
        resource_type="claim_journal_receipt",
        resource_id=f"{tracker}:{ticket_id}:{role}",
        details=details,
    )


def record_plugin_install_receipt(
    *,
    chain: AuditChainStore,
    skill_hash: str,
    manifest_hash: str,
    install_id: str,
    spine_anchor: str,
    host: str,
    scope: str,
    dest: str,
    actor: str = "skill_packaging",
) -> AuditEvent:
    """Append a ``plugin.install_receipt`` event into *chain* (#2369).

    Records the packaged agent-skill / plugin install receipt: the installed
    tree's content address, the manifest hash the receipt binds to, and the
    lineage-spine anchor of the receipt row. Together with the receipt file
    under ``.sdd/skills/receipts/`` this lets a verifier prove -- from the
    chain alone -- which skill content an agent host was driving.

    Args:
        chain: The audit chain store accepting the entry.
        skill_hash: Content address of the installed tree (``sha256:<hex>``).
        manifest_hash: SHA-256 of the installed manifest file (``SKILL.md``
            or the plugin manifest).
        install_id: Per-install identifier tying this event to the receipt.
        spine_anchor: Entry hash of the receipt row in the install lineage
            spine; a verifier holding the spine can recompute it.
        host: Target agent host (``claude`` / ``codex`` / ... / ``dest``).
        scope: Install scope (``project`` / ``user`` / ``dest``).
        dest: Destination directory the tree was installed into.
        actor: Recorded actor; defaults to ``"skill_packaging"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded
        in its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_PLUGIN_INSTALL_RECEIPT,
        actor=actor,
        resource_type="plugin_install_receipt",
        resource_id=skill_hash,
        details={
            "skill_hash": skill_hash,
            "manifest_hash": manifest_hash,
            "install_id": install_id,
            "spine_anchor": spine_anchor,
            "host": host,
            "scope": scope,
            "dest": dest,
        },
    )


def record_plugin_update_receipt(
    *,
    chain: AuditChainStore,
    prior_skill_hash: str,
    skill_hash: str,
    manifest_hash: str,
    install_id: str,
    spine_anchor: str,
    host: str,
    scope: str,
    dest: str,
    actor: str = "skill_packaging",
) -> AuditEvent:
    """Append a ``plugin.update_receipt`` event into *chain* (#2369, tail).

    Records that a previously attested packaged install at *dest* was
    superseded: the tree that was there (``prior_skill_hash``) was replaced
    by new content (``skill_hash``). The event binds both addresses plus the
    lineage-spine anchor of the update-receipt row. A verifier holding the
    chain walks update receipts from the current content address back through
    their ``prior_skill_hash`` links until it reaches the root
    ``plugin.install_receipt`` -- the full supersession history of an
    installed tree is reconstructable offline.

    Args:
        chain: The audit chain store accepting the entry.
        prior_skill_hash: Content address of the tree being superseded
            (``sha256:<hex>``).
        skill_hash: Content address of the new installed tree.
        manifest_hash: SHA-256 of the new installed manifest file.
        install_id: Per-install identifier tying this event to the receipt.
        spine_anchor: Entry hash of the update-receipt row in the install
            lineage spine.
        host: Target agent host (``claude`` / ``codex`` / ... / ``dest``).
        scope: Install scope (``project`` / ``user`` / ``dest``).
        dest: Destination directory the tree was updated in place.
        actor: Recorded actor; defaults to ``"skill_packaging"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded
        in its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_PLUGIN_UPDATE_RECEIPT,
        actor=actor,
        resource_type="plugin_update_receipt",
        resource_id=skill_hash,
        details={
            "prior_skill_hash": prior_skill_hash,
            "skill_hash": skill_hash,
            "manifest_hash": manifest_hash,
            "install_id": install_id,
            "spine_anchor": spine_anchor,
            "host": host,
            "scope": scope,
            "dest": dest,
        },
    )


def record_plugin_conformance_receipt(
    *,
    chain: AuditChainStore,
    skill_hash: str,
    receipt_id: str,
    host_results: list[tuple[str, bool]],
    min_hosts: int,
    passed_hosts: int,
    ok: bool,
    install_id: str,
    spine_anchor: str,
    actor: str = "skill_packaging",
) -> AuditEvent:
    """Append a ``plugin.conformance_receipt`` event into *chain* (#2369, tail).

    Records one multi-host conformance sweep of a packaged skill: the shared
    installed content address, the ordered per-host pass/fail verdicts, the
    content id of the sealed conformance receipt, and its lineage-spine
    anchor. Together with the receipt file under ``.sdd/skills/conformance/``
    a verifier can prove -- from the chain alone -- that one skill content
    address drove ``passed_hosts`` distinct agent hosts against one install
    and whether the ``min_hosts`` bar was met.

    Args:
        chain: The audit chain store accepting the entry.
        skill_hash: Content address shared by every host install
            (``sha256:<hex>``).
        receipt_id: Content address of the conformance receipt itself.
        host_results: Ordered ``(host, ok)`` verdicts (sorted by host).
        min_hosts: Minimum number of green hosts the sweep required.
        passed_hosts: Number of hosts whose contract passed.
        ok: Aggregate verdict (all hosts green and at least ``min_hosts``).
        install_id: Per-sweep identifier tying this event to the receipt.
        spine_anchor: Entry hash of the receipt row in the install lineage
            spine; a verifier holding the spine can recompute it.
        actor: Recorded actor; defaults to ``"skill_packaging"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded
        in its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_PLUGIN_CONFORMANCE_RECEIPT,
        actor=actor,
        resource_type="plugin_conformance_receipt",
        resource_id=receipt_id,
        details={
            "skill_hash": skill_hash,
            "receipt_id": receipt_id,
            "host_results": [[host, verdict] for host, verdict in host_results],
            "min_hosts": min_hosts,
            "passed_hosts": passed_hosts,
            "ok": ok,
            "install_id": install_id,
            "spine_anchor": spine_anchor,
        },
    )


def record_adapter_canary_receipt(
    *,
    chain: AuditChainStore,
    adapter: str,
    binary: str,
    installed_version: str | None,
    verdict: str,
    receipt_sha256: str,
    failures: list[str],
    actor: str = "adapter_canary",
) -> AuditEvent:
    """Append an ``adapter.canary_receipt`` event into *chain* (#2368).

    Mirrors one canary probe into the HMAC chain: the probed adapter, the
    upstream version discovered on the runner, the conformance verdict,
    and the content hash of the sealed canary receipt. A verifier holding
    the receipt file can recompute its hash and check it against the
    chain, so a canary finding (and the last-green table row it attests)
    cannot be forged by editing the artifact.

    Args:
        chain: The audit chain store accepting the entry.
        adapter: Adapter registry key probed.
        binary: Binary name the probe resolved.
        installed_version: Parsed upstream version, or ``None`` when
            unknown.
        verdict: ``pass`` / ``fail`` / ``skip``.
        receipt_sha256: Content hash of the canonical receipt bytes.
        failures: Conformance failure lines (empty unless ``fail``).
        actor: Recorded actor; defaults to ``"adapter_canary"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest``
        embedded in its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_ADAPTER_CANARY_RECEIPT,
        actor=actor,
        resource_type="adapter_canary",
        resource_id=adapter,
        details={
            "adapter": adapter,
            "binary": binary,
            "installed_version": installed_version,
            "verdict": verdict,
            "receipt_sha256": receipt_sha256,
            "failures": failures.copy(),
        },
    )


def record_adapter_admission_receipt(
    *,
    chain: AuditChainStore,
    adapter: str,
    binary: str,
    installed_version: str | None,
    contract_hash: str,
    replay_fingerprint: str,
    conformance_run_id: str,
    verdict: str,
    reason: str,
    allowed_capabilities: list[str],
    forbidden_capabilities: list[str],
    receipt_sha256: str,
    kind: str = EVENT_ADAPTER_ADMISSION_RECEIPT,
    actor: str = "adapter_admission",
) -> AuditEvent:
    """Append an ``adapter.admission_receipt`` event into *chain* (#2610).

    Mirrors one admission decision into the HMAC chain: the adapter, the
    upstream version it was probed against, the pinned contract's content
    hash, the deterministic replay fingerprint, the conformance run that
    derived it, and the capability split the decision granted or withheld.

    Refusals are recorded on exactly the same terms as admissions, which is
    what makes the record load-bearing. A conformance verdict of ``skip``
    that produced no event would leave an unverified adapter looking
    indistinguishable from an unexamined one; with the event, a verifier
    holding a contiguous chain slice can prove offline which adapters held
    spawn authority during a window and, for each one that did not, the reason
    and the capabilities it was denied.

    Args:
        chain: The audit chain store accepting the entry.
        adapter: Adapter registry key.
        binary: Binary name the admission probe resolved.
        installed_version: Captured upstream version, or ``None``.
        contract_hash: SHA-256 of the pinned contract bytes, ``""`` when the
            adapter ships none.
        replay_fingerprint: Deterministic projection of the contract bytes,
            the binary version, and the golden-transcript replay output.
        conformance_run_id: Deterministic id of the conformance run behind the
            decision.
        verdict: ``admit`` or ``refuse``.
        reason: Refusal reason; empty on an admission.
        allowed_capabilities: Capability axes the decision grants.
        forbidden_capabilities: Capability axes the decision withholds.
        receipt_sha256: Content hash of the canonical receipt bytes.
        kind: The receipt ``kind`` discriminator (sealed admission receipt vs
            gate decision), recorded so the two are never conflated.
        actor: Recorded actor; defaults to ``"adapter_admission"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded in
        its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_ADAPTER_ADMISSION_RECEIPT,
        actor=actor,
        resource_type="adapter_admission",
        resource_id=adapter,
        details={
            "adapter": adapter,
            "binary": binary,
            "installed_version": installed_version,
            "contract_hash": contract_hash,
            "replay_fingerprint": replay_fingerprint,
            "conformance_run_id": conformance_run_id,
            "verdict": verdict,
            "reason": reason,
            "allowed_capabilities": allowed_capabilities.copy(),
            "forbidden_capabilities": forbidden_capabilities.copy(),
            "receipt_sha256": receipt_sha256,
            "kind": kind,
        },
    )


def record_capability_selection(
    *,
    chain: AuditChainStore,
    run_id: str,
    adapter: str,
    profile_hash: str,
    requirements: dict[str, Any],
    actor: str = "capability_router",
) -> AuditEvent:
    """Append an ``adapter.capability_selection`` event into *chain* (#2663).

    Mirrors one capability-aware routing decision into the HMAC chain: the
    adapter chosen for a task and the content-addressed capability profile it
    presented at dispatch. The recorded ``profile_hash`` is a pure function of
    the adapter's declaration, so a verifier replaying the run recomputes it and
    detects a changed declaration as a hash divergence named by the adapter --
    profile drift becomes tamper-evident rather than an unexplained behaviour
    change. Only names, hashes, and the task's declared requirements are
    recorded; never a prompt or a spawn command.

    Args:
        chain: The audit chain store accepting the entry.
        run_id: The run the routing decision was made for.
        adapter: Registry key of the selected adapter.
        profile_hash: Content address of the capability profile the adapter
            presented (its :attr:`profile_hash`).
        requirements: Canonical form of the task requirements the profile
            satisfied.
        actor: Recorded actor; defaults to ``"capability_router"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded in
        its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_ADAPTER_CAPABILITY_SELECTION,
        actor=actor,
        resource_type="adapter_capability_selection",
        resource_id=adapter,
        details={
            "run_id": run_id,
            "adapter": adapter,
            "profile_hash": profile_hash,
            "requirements": dict(sorted(requirements.items())),
        },
    )


def record_capability_refusal(
    *,
    chain: AuditChainStore,
    run_id: str,
    receipt_hash: str,
    requirements: dict[str, Any],
    candidates: list[list[str]],
    unmet: list[str],
    actor: str = "capability_router",
) -> AuditEvent:
    """Append an ``adapter.capability_refusal`` event into *chain* (#2663).

    Anchors one capability-aware routing refusal: no candidate adapter's
    declared profile satisfied the task, so routing refuses rather than falling
    back to a weaker adapter. The event mirrors the content-addressed refusal
    receipt -- its hash, the union of unmet axes, and every candidate considered
    paired with the profile hash it presented -- into the HMAC chain. A verifier
    holding the receipt can recompute its hash and check it against the chain,
    so the refusal is a signed, reconstructable record an operator can hand to a
    postmortem rather than a decision that left no trace.

    Args:
        chain: The audit chain store accepting the entry.
        run_id: The run the refusal was raised for.
        receipt_hash: Content address of the
            :class:`~bernstein.adapters.capability_profile.CapabilityRefusalReceipt`.
        requirements: Canonical form of the task requirements that went unmet.
        candidates: ``[adapter name, profile hash]`` pairs considered, in the
            order they were offered.
        unmet: Sorted union of every unmet capability axis across candidates.
        actor: Recorded actor; defaults to ``"capability_router"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded in
        its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_ADAPTER_CAPABILITY_REFUSAL,
        actor=actor,
        resource_type="adapter_capability_refusal",
        resource_id=receipt_hash,
        details={
            "run_id": run_id,
            "receipt_hash": receipt_hash,
            "requirements": dict(sorted(requirements.items())),
            "candidates": [list(pair) for pair in candidates],
            "unmet": list(unmet),
        },
    )


def record_adapter_spawn_preflight_receipt(
    *,
    chain: AuditChainStore,
    adapter: str,
    binary: str,
    installed_version: str | None,
    floor: str | None,
    advisory_id: str | None,
    verdict: str,
    blocked: bool,
    policy: str,
    floor_map_hash: str,
    receipt_sha256: str,
    actor: str = "spawner",
) -> AuditEvent:
    """Append an ``adapter.spawn_preflight_receipt`` event into *chain* (#2515).

    Mirrors one spawn-time security-floor decision into the HMAC chain: the
    probed adapter, the installed upstream version, the minimum-safe floor, the
    advisory id, the enforcement policy, the floor map's content hash, and the
    content hash of the sealed receipt. Permits carry the verdict too, so a
    verifier holding a contiguous chain slice can prove offline that no
    below-floor adapter spawn was permitted during a window -- a refusal-only
    record would prove nothing about the windows in between. Because the
    receipt pins the floor map's content hash, a floor map mutated after the
    fact is caught at verification as a hash mismatch.

    Args:
        chain: The audit chain store accepting the entry.
        adapter: Adapter registry key probed.
        binary: Binary name the floor was probed from.
        installed_version: Parsed upstream version, or ``None`` when unknown.
        floor: The minimum-safe floor, or ``None`` when the adapter is
            untracked.
        advisory_id: Bernstein-local advisory id, or ``None`` when untracked.
        verdict: ``permit`` / ``refuse`` / ``warn_override`` /
            ``unknown_version``.
        blocked: Whether the spawn was refused.
        policy: Enforcement policy in force (``block`` / ``warn``).
        floor_map_hash: Content hash of the floor map at decision time.
        receipt_sha256: Content hash of the canonical receipt bytes.
        actor: Recorded actor; defaults to ``"spawner"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded in
        its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_ADAPTER_SPAWN_PREFLIGHT,
        actor=actor,
        resource_type="adapter_spawn_preflight",
        resource_id=adapter,
        details={
            "adapter": adapter,
            "binary": binary,
            "installed_version": installed_version,
            "floor": floor,
            "advisory_id": advisory_id,
            "verdict": verdict,
            "blocked": blocked,
            "policy": policy,
            "floor_map_hash": floor_map_hash,
            "receipt_sha256": receipt_sha256,
        },
    )


def record_adapter_version_posture_receipt(
    *,
    chain: AuditChainStore,
    receipt_sha256: str,
    floor_map_hash: str,
    entries: list[dict[str, Any]],
    actor: str = "doctor",
) -> AuditEvent:
    """Append an ``adapter.version_posture`` event into *chain* (#2515).

    Mirrors one ``bernstein doctor`` version-posture snapshot into the HMAC
    chain: the content hash of the sealed posture receipt, the floor map's
    content hash, and a compact per-adapter verdict summary. Turning the
    doctor's version posture from console text into a chain-anchored receipt
    makes "only floor-satisfying binaries were spawnable in this environment
    during window X" provable offline from a contiguous chain slice.

    Args:
        chain: The audit chain store accepting the entry.
        receipt_sha256: Content hash of the canonical posture-receipt bytes.
        floor_map_hash: Content hash of the floor map at snapshot time.
        entries: Per-adapter posture rows (adapter, installed_version, floor,
            advisory_id, verdict). Copied into the event details.
        actor: Recorded actor; defaults to ``"doctor"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded in
        its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_ADAPTER_VERSION_POSTURE,
        actor=actor,
        resource_type="adapter_version_posture",
        resource_id=receipt_sha256,
        details={
            "receipt_sha256": receipt_sha256,
            "floor_map_hash": floor_map_hash,
            "entries": [entry.copy() for entry in entries],
        },
    )


def record_adapter_floor_update_receipt(
    *,
    chain: AuditChainStore,
    receipt_sha256: str,
    old_floor_map_hash: str,
    new_floor_map_hash: str,
    diff: dict[str, Any],
    actor: str = "floor_refresh",
) -> AuditEvent:
    """Append an ``adapter.floor_update_receipt`` event into *chain* (#2515).

    Mirrors one security-floor map refresh into the HMAC chain: the content
    hash of the sealed update receipt, the old and new floor-map content
    hashes, and the data-only diff. A floor bump becomes an attested event, so
    a reviewer can prove offline which floor map was in force when a
    spawn-preflight or version-posture receipt (which pin the same content
    hash) was recorded.

    Args:
        chain: The audit chain store accepting the entry.
        receipt_sha256: Content hash of the canonical update-receipt bytes.
        old_floor_map_hash: Content hash of the floor map before the bump.
        new_floor_map_hash: Content hash of the floor map after the bump.
        diff: The data-only diff (added / removed / changed).
        actor: Recorded actor; defaults to ``"floor_refresh"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded in
        its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_ADAPTER_FLOOR_UPDATE,
        actor=actor,
        resource_type="adapter_floor_update",
        resource_id=receipt_sha256,
        details={
            "receipt_sha256": receipt_sha256,
            "old_floor_map_hash": old_floor_map_hash,
            "new_floor_map_hash": new_floor_map_hash,
            "diff": diff,
        },
    )


def record_process_reap_receipt(
    *,
    chain: AuditChainStore,
    session_id: str,
    pgid: int,
    os_name: str,
    method: str,
    delivered: bool,
    escalated: bool,
    grace_seconds: float,
    reason: str,
    actor: str = "spawner",
) -> AuditEvent:
    """Append a ``process.reap_receipt`` event into *chain* (#2367).

    Mirrors a forced agent process-tree reap into the audit chain.  The
    receipt records which platform mechanism delivered the stop (POSIX
    process-group signalling or Windows process-tree termination), whether
    the graceful stop was delivered, and whether escalation to a force-kill
    was required.  A verifier reconstructing a failure window can prove
    offline which reap path ran instead of inferring it from log lines.

    Args:
        chain: The audit chain store accepting the entry.
        session_id: Agent session whose process tree was reaped.
        pgid: Process group ID (POSIX) or lead PID (Windows) targeted.
        os_name: Normalised OS name (``"linux"``/``"macos"``/``"windows"``).
        method: Delivery mechanism identifier
            (``"posix_process_group"`` / ``"windows_process_tree"``).
        delivered: Whether the initial graceful stop was delivered.
        escalated: Whether a force-kill was required after the grace window.
        grace_seconds: The grace window that applied to this reap.
        reason: Why the reap ran (e.g. ``"kill_requested"``,
            ``"heartbeat_stale"``, ``"wall_clock_timeout"``).
        actor: Recorded actor; defaults to ``"spawner"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded
        in its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_PROCESS_REAP_RECEIPT,
        actor=actor,
        resource_type="process_reap",
        resource_id=session_id,
        details={
            "session_id": session_id,
            "pgid": pgid,
            "os_name": os_name,
            "method": method,
            "delivered": delivered,
            "escalated": escalated,
            "grace_seconds": grace_seconds,
            "reason": reason,
        },
    )


def record_task_suspension(
    *,
    chain: AuditChainStore,
    task_id: str,
    suspend_event_hash: str,
    journal_index: int,
    adapter: str,
    workspace_hash: str,
    envelope: str,
    reserved_usd: float,
    spent_usd: float,
    released_usd: float,
    wake_condition: str = "",
    actor: str = "suspension",
) -> AuditEvent:
    """Append a ``task.suspend_receipt`` event into *chain* (#2552).

    Binds the suspend journal row's Merkle head -- the suspension's identity --
    into the HMAC chain *before any infrastructure release runs*. The receipt's
    own HMAC (``AuditEvent.hmac``) is the value each release effect references,
    so seat return, sandbox teardown, process reap, and envelope-headroom
    release all hang off a receipt that already exists on the chain. Only
    identifiers, hashes, and the envelope balance are recorded -- never prompt
    content or provider session state.

    Args:
        chain: The audit chain store accepting the entry.
        task_id: The task being parked.
        suspend_event_hash: Merkle hash of the suspend row in the task's event
            journal (the suspension's identity).
        journal_index: 0-based journal index of that row.
        adapter: Registry name of the adapter that owned the parked session.
        workspace_hash: Content hash of the worktree at park time.
        envelope: Quota envelope whose headroom is being released.
        reserved_usd: Envelope headroom reserved for the task at park time.
        spent_usd: Recorded spend against the reservation at park time.
        released_usd: Headroom released back to the pool
            (``max(reserved - spent, 0)``).
        wake_condition: Optional wake gate (``"approval"`` for
            ``--until approval``); empty for an operator-driven resume.
        actor: Recorded actor; defaults to ``"suspension"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded in
        its details payload; its ``hmac`` is the suspend receipt hash.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_TASK_SUSPENDED,
        actor=actor,
        resource_type="task_suspension",
        resource_id=task_id,
        details={
            "task_id": task_id,
            "suspend_event_hash": suspend_event_hash,
            "journal_index": journal_index,
            "adapter": adapter,
            "workspace_hash": workspace_hash,
            "envelope": envelope,
            "reserved_usd": round(reserved_usd, 6),
            "spent_usd": round(spent_usd, 6),
            "released_usd": round(released_usd, 6),
            "wake_condition": wake_condition,
        },
    )


def record_task_resource_release(
    *,
    chain: AuditChainStore,
    task_id: str,
    resource: str,
    suspend_receipt_hash: str,
    detail: dict[str, Any] | None = None,
    actor: str = "suspension",
) -> AuditEvent:
    """Append a ``task.suspend_resource_release`` event into *chain* (#2552).

    One release row per freed resource (``seat`` / ``sandbox`` / ``process`` /
    ``budget``). Every row references the ``suspend_receipt_hash`` the release
    hangs off; the caller refuses to emit the row (and to run the physical
    effect) when that hash is missing, so a release with no matching receipt
    never reaches the chain -- fail closed.

    Args:
        chain: The audit chain store accepting the entry.
        task_id: The parked task whose resource was freed.
        resource: Resource kind (``"seat"`` / ``"sandbox"`` / ``"process"`` /
            ``"budget"``).
        suspend_receipt_hash: HMAC of the ``task.suspend_receipt`` this release
            references (never empty).
        detail: Optional resource-specific detail (e.g. released USD, sandbox
            backend). Recorded verbatim.
        actor: Recorded actor; defaults to ``"suspension"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded in
        its details payload.
    """
    payload: dict[str, Any] = {
        "task_id": task_id,
        "resource": resource,
        "suspend_receipt_hash": suspend_receipt_hash,
    }
    if detail:
        payload["detail"] = detail
    return chain.log_with_prev_digest(
        event_type=EVENT_TASK_RESOURCE_RELEASE,
        actor=actor,
        resource_type="task_resource_release",
        resource_id=task_id,
        details=payload,
    )


def record_task_resume(
    *,
    chain: AuditChainStore,
    task_id: str,
    suspend_receipt_hash: str,
    suspend_event_hash: str,
    resume_event_hash: str,
    journal_index: int,
    effective_mode: str,
    requested_mode: str,
    workspace_match: bool,
    new_workspace_hash: str,
    downgrade_reason: str,
    decision_hash: str,
    approval_ref: str = "",
    actor: str = "suspension",
) -> AuditEvent:
    """Append a ``task.resume_receipt`` event into *chain* (#2552).

    Closes the continuity proof: the resume receipt binds the suspend receipt
    it continued from, the suspend row hash, the effective continuation mode,
    and the new workspace hash. A verifier holding a copied chain confirms the
    resumed run continued from exactly the parked workspace hash (``warm``),
    or reads the recorded ``fork`` / ``cold`` downgrade with its reason. When
    the park was gated ``--until approval`` the approval decision digest is
    bound here so the approval record and the resume receipt reference each
    other.

    Args:
        chain: The audit chain store accepting the entry.
        task_id: The task being resumed.
        suspend_receipt_hash: HMAC of the ``task.suspend_receipt`` this resume
            continued from.
        suspend_event_hash: Merkle hash of the suspend journal row.
        resume_event_hash: Merkle hash of the resume journal row.
        journal_index: 0-based journal index of the resume row.
        effective_mode: Effective continuation mode (``warm`` / ``fork`` /
            ``cold``).
        requested_mode: The mode the operator requested.
        workspace_match: Whether the live workspace hash matched the parked
            hash at resume time.
        new_workspace_hash: Content hash of the re-materialized worktree.
        downgrade_reason: Why a non-cold request became fork/cold; empty when
            the request was honored warm.
        decision_hash: SHA-256 of the canonical resume decision projection.
        approval_ref: Digest of the approval decision for an
            ``--until approval`` park; empty otherwise.
        actor: Recorded actor; defaults to ``"suspension"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded in
        its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_TASK_RESUMED,
        actor=actor,
        resource_type="task_resume",
        resource_id=task_id,
        details={
            "task_id": task_id,
            "suspend_receipt_hash": suspend_receipt_hash,
            "suspend_event_hash": suspend_event_hash,
            "resume_event_hash": resume_event_hash,
            "journal_index": journal_index,
            "effective_mode": effective_mode,
            "requested_mode": requested_mode,
            "workspace_match": workspace_match,
            "new_workspace_hash": new_workspace_hash,
            "downgrade_reason": downgrade_reason,
            "decision_hash": decision_hash,
            "approval_ref": approval_ref,
        },
    )


def record_dashboard_token_grant(
    *,
    chain: AuditChainStore,
    grant: str,
    token_id: str,
    token_sha256: str,
    principal: str,
    scope: str,
    actor: str = "dashboard",
) -> AuditEvent:
    """Append a ``dashboard.token_grant`` event into *chain* (#2366).

    Mirrors one signed row of the dashboard token registry so credential
    grants and revocations are chain-attested alongside the authz decisions
    they later authorize. Only the digest and metadata are recorded -- the
    raw token exists solely in the issuing terminal.

    Args:
        chain: The audit chain store accepting the entry.
        grant: ``issue`` or ``revoke``.
        token_id: Short hex id of the token (digest prefix).
        token_sha256: Hex SHA-256 of the raw token.
        principal: The seat / person the token attributes actions to.
        scope: The granted scope (``viewer`` / ``operator``; empty on
            revocations).
        actor: Recorded actor; defaults to ``"dashboard"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded
        in its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_DASHBOARD_TOKEN_GRANT,
        actor=actor,
        resource_type="dashboard_token",
        resource_id=token_id,
        details={
            "grant": grant,
            "token_id": token_id,
            "token_sha256": token_sha256,
            "principal": principal,
            "scope": scope,
        },
    )


def record_cost_dispatch_receipt(
    *,
    chain: AuditChainStore,
    decision_hash: str,
    run_id: str,
    task_id: str,
    admit: bool,
    breached_dimension: str,
    projected_overrun_usd: float,
    price_table_hash: str,
    ledger_state_hash: str,
    policy_hash: str,
    journal_entry_hash: str,
    knob_selection_hash: str = "",
    actor: str = "cost_policy",
) -> AuditEvent:
    """Append a ``cost.dispatch_receipt`` event into *chain* (#2354).

    Mirrors one deterministic cost-aware dispatch decision into the HMAC chain
    so an operator can prove, from the chain alone, that a dispatch was admitted
    or halted under a named policy against a pinned price table and ledger --
    and, on a halt, exactly which dimension breached and by how much. Only
    hashes, the verdict, and the projected overrun are recorded; a verifier
    holding the same ledger and price table recomputes the ``decision_hash``
    byte-identically.

    Args:
        chain: The audit chain store accepting the entry.
        decision_hash: Deterministic ``sha256:`` hash pinning the whole
            decision (the receipt identity).
        run_id: The run the candidate belonged to.
        task_id: The task the candidate belonged to.
        admit: Whether the dispatch was admitted (``False`` == halted).
        breached_dimension: The first breached cap dimension (``task`` /
            ``run`` / ``day``), empty when admitted.
        projected_overrun_usd: USD the breached dimension would exceed its cap
            by (``0`` when admitted).
        price_table_hash: Content hash of the pinned price table.
        ledger_state_hash: Hash over the projected prior spend the decision
            read from the ledger.
        policy_hash: Content hash of the caps the decision enforced.
        journal_entry_hash: Lineage-spine entry hash anchoring the sealed
            decision bytes; a verifier holding the spine can recompute it.
        knob_selection_hash: Content hash of the sealed dispatch knob selection
            (effort, lane, cache strategy, multiplier). Recorded only when a
            knob matrix resolved the dispatch, so a verifier can pin the exact
            knob configuration the decision used (#2519). Empty when no matrix
            was consulted (back-compat).
        actor: Recorded actor; defaults to ``"cost_policy"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded in
        its details payload.
    """
    details: dict[str, Any] = {
        "decision_hash": decision_hash,
        "run_id": run_id,
        "task_id": task_id,
        "admit": admit,
        "breached_dimension": breached_dimension,
        "projected_overrun_usd": round(projected_overrun_usd, 6),
        "price_table_hash": price_table_hash,
        "ledger_state_hash": ledger_state_hash,
        "policy_hash": policy_hash,
        "journal_entry_hash": journal_entry_hash,
    }
    if knob_selection_hash:
        details["knob_selection_hash"] = knob_selection_hash
    return chain.log_with_prev_digest(
        event_type=EVENT_COST_DISPATCH_RECEIPT,
        actor=actor,
        resource_type="cost_dispatch_receipt",
        resource_id=decision_hash,
        details=details,
    )


def record_tournament_selection(
    *,
    chain: AuditChainStore,
    task_id: str,
    receipt_hash: str,
    winner_hash: str,
    attempt_hashes: list[str],
    evaluator_names: list[str],
    actor: str = "tournament",
) -> AuditEvent:
    """Append a ``tournament.selection`` event into *chain* (#2353).

    Mirrors a signed tournament selection receipt into the audit chain. The
    receipt itself is the offline-verifiable proof of why an attempt won; this
    entry lets an auditor join the chain to that receipt and confirm the
    decision was made without a model in the loop. Only hashes and metadata are
    recorded -- the attempt artefacts live in the tournament lineage spine.

    Args:
        chain: The audit chain store accepting the entry.
        task_id: The task the tournament ran for.
        receipt_hash: The tournament receipt's spine anchor (its identity).
        winner_hash: The winning attempt's content hash.
        attempt_hashes: Every attempt's content hash (winner + losers).
        evaluator_names: The scripted evaluators that decided the outcome.
        actor: Recorded actor; defaults to ``"tournament"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded
        in its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_TOURNAMENT_SELECTION,
        actor=actor,
        resource_type="tournament",
        resource_id=task_id,
        details={
            "task_id": task_id,
            "receipt_hash": receipt_hash,
            "winner_hash": winner_hash,
            "attempt_hashes": [*attempt_hashes],
            "attempt_count": len(attempt_hashes),
            "evaluator_names": [*evaluator_names],
        },
    )


def record_spec_requirement_set(
    *,
    chain: AuditChainStore,
    requirement_set_hash: str,
    source_hash: str,
    requirement_count: int,
    graph_hash: str,
    decision: str,
    actor: str = "spec-pipeline",
) -> AuditEvent:
    """Append a ``spec.requirement_set`` approval receipt into *chain* (#2361).

    The receipt is the plan-approval gate for the spec pipeline. It binds the
    content-addressed requirement-set hash, the source-spec hash, the compiled
    graph hash, and the decision into the HMAC chain so a verifier can prove,
    from the chain alone, that a task graph was compiled from the exact
    requirement set the operator approved. Only hashes and metadata are
    recorded -- never the spec body or the requirement text.

    Args:
        chain: The audit chain store accepting the entry.
        requirement_set_hash: ``sha256:`` digest over the ordered
            ``(id, line_hash)`` pairs of the approved requirement set.
        source_hash: ``sha256:`` digest of the source spec document.
        requirement_count: Number of requirements in the approved set.
        graph_hash: ``sha256:`` digest of the compiled task graph.
        decision: ``approved`` or ``rejected``.
        actor: Recorded actor; defaults to ``"spec-pipeline"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded
        in its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_SPEC_REQUIREMENT_SET,
        actor=actor,
        resource_type="requirement_set",
        resource_id=requirement_set_hash,
        details={
            "requirement_set_hash": requirement_set_hash,
            "source_hash": source_hash,
            "requirement_count": requirement_count,
            "graph_hash": graph_hash,
            "decision": decision,
        },
    )


def record_spiffe_svid_binding(
    *,
    chain: AuditChainStore,
    agent_id: str,
    spiffe_id: str,
    install_id: str,
    card_hash: str,
    svid_sha256: str,
    binding_hash: str,
    trust_domain: str,
    actor: str = "workload_identity",
) -> AuditEvent:
    """Append a ``spiffe.svid_binding`` event into *chain* (#2363).

    Anchors the mapping between a SPIRE-issued X.509-SVID and an agent card:
    the derived SPIFFE ID, the install fingerprint the ID derives from, the
    card hash at binding time, the leaf SVID's content address, and the
    binding's own content hash. Records identifiers and hashes only -- never
    the SVID private key -- so the receipt is safe to chain and a verifier
    holding the chain plus the install public key can prove the binding offline.

    Args:
        chain: The audit chain store accepting the entry.
        agent_id: The bound agent card's id.
        spiffe_id: The derived ``spiffe://`` id both card and SVID carry.
        install_id: The install fingerprint segment the SPIFFE ID derives from.
        card_hash: The card's ``card_hash`` at binding time.
        svid_sha256: Content address (``sha256:<hex>``) of the leaf SVID DER.
        binding_hash: Content address of the binding's canonical identity.
        trust_domain: The SPIFFE trust domain the id lives in.
        actor: Recorded actor; defaults to ``"workload_identity"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded in
        its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_SPIFFE_SVID_BINDING,
        actor=actor,
        resource_type="spiffe_svid_binding",
        resource_id=spiffe_id,
        details={
            "agent_id": agent_id,
            "spiffe_id": spiffe_id,
            "install_id": install_id,
            "card_hash": card_hash,
            "svid_sha256": svid_sha256,
            "binding_hash": binding_hash,
            "trust_domain": trust_domain,
        },
    )


def record_mcp_task_handle(
    *,
    chain: AuditChainStore,
    task_id: str,
    run_id: str,
    status: str,
    journal_head: str,
    chain_head: str,
    receipt_hash: str,
    spec_revision: str,
    trace_id: str = "",
    actor: str = "mcp_task_handle",
) -> AuditEvent:
    """Append an ``mcp.task_handle`` event into *chain* (#2364).

    Anchors an MCP Tasks-extension run handle in the audit chain. The handle
    is a pure projection of the run journal; recording its receipt hash, the
    run journal head, and the embedded chain head means a client that polled
    the handle can later prove offline that the task it watched corresponds
    to the audited run: recompute the receipt from the journal and match the
    embedded chain head against a verified chain.

    Args:
        chain: The audit chain store accepting the entry.
        task_id: The Tasks-extension task id.
        run_id: The run whose journal the handle projects.
        status: The projected Tasks-extension status.
        journal_head: The run journal's Merkle head hash.
        chain_head: The audit-chain head hash embedded in the handle.
        receipt_hash: The content-addressed digest of the handle.
        spec_revision: The pinned Tasks-extension revision.
        trace_id: The ingested W3C trace id, empty when none.
        actor: Recorded actor; defaults to ``"mcp_task_handle"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded
        in its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_MCP_TASK_HANDLE,
        actor=actor,
        resource_type="mcp_task_handle",
        resource_id=run_id,
        details={
            "task_id": task_id,
            "run_id": run_id,
            "status": status,
            "journal_head": journal_head,
            "chain_head": chain_head,
            "receipt_hash": receipt_hash,
            "spec_revision": spec_revision,
            "trace_id": trace_id,
        },
    )


def record_run_lifecycle(
    *,
    chain: AuditChainStore,
    run_id: str,
    transition: str,
    ledger_head: str,
    entry_count: int,
    from_head: str = "",
    to_head: str = "",
    entries_added: int = 0,
    actor: str = "run_service",
) -> AuditEvent:
    """Append a ``run.lifecycle`` event into *chain* (#2352).

    Records one lifecycle boundary of a detached run into the HMAC-chained
    audit log. The detached-run daemon owns execution but its state is a
    projection of the durable work ledger; this receipt binds the ledger head
    at the boundary so a reattaching operator (or an offline verifier) can
    prove the current ledger extends the head last seen. Only identifiers,
    the transition, and the continuity span are recorded -- never goal text or
    task payloads.

    Args:
        chain: The audit chain store accepting the entry.
        run_id: The run whose lifecycle boundary is recorded.
        transition: One of ``submitted`` / ``detached`` / ``reattached`` /
            ``daemon_restarted`` / ``completed``.
        ledger_head: Head entry hash of the work ledger at the boundary.
        entry_count: Number of ledger entries at the boundary.
        from_head: Ledger head the operator last saw (reattach/restart only).
        to_head: Ledger head at reattach/restart (equals ``ledger_head``).
        entries_added: Entries appended between ``from_head`` and ``to_head``.
        actor: Recorded actor; defaults to ``"run_service"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded
        in its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_RUN_LIFECYCLE,
        actor=actor,
        resource_type="run",
        resource_id=run_id,
        details={
            "run_id": run_id,
            "transition": transition,
            "ledger_head": ledger_head,
            "entry_count": entry_count,
            "from_head": from_head,
            "to_head": to_head,
            "entries_added": entries_added,
        },
    )


def record_review_board_action(
    *,
    chain: AuditChainStore,
    run_id: str,
    task_id: str,
    decision: str,
    principal: str,
    scope: str,
    projection_hash: str,
    journal_head: str,
    diff_hash: str,
    journal_entry_hash: str,
    actor: str = "review_board",
) -> AuditEvent:
    """Append a ``review_board.action`` event into *chain*.

    Mirrors one signed operator board decision into the HMAC-chained audit
    log. The receipt names the acting principal and binds what was reviewed:
    the board ``projection_hash`` the operator saw, the run ``journal_head``
    the decision chained onto, and the reviewed ``diff_hash``. Only hashes,
    the decision, the principal, and the scope are recorded -- never diff
    bytes. A verifier holding the run journal can prove the decision row at
    ``journal_entry_hash`` follows the named head.

    Args:
        chain: The audit chain store accepting the entry.
        run_id: The run the reviewed task belongs to.
        task_id: The reviewed task.
        decision: The board action (``approve`` / ``request_changes`` /
            ``merge``).
        principal: The acting operator principal (dashboard-auth credential).
        scope: The scope the action was authorized under.
        projection_hash: The board projection hash the operator saw.
        journal_head: The run journal Merkle head the decision chained onto.
        diff_hash: Content hash of the reviewed task diff (empty when none).
        journal_entry_hash: The recorded decision row's ``event_hash``.
        actor: Recorded actor; defaults to ``"review_board"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded
        in its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_REVIEW_BOARD_ACTION,
        actor=actor,
        resource_type="review_board_action",
        resource_id=task_id,
        details={
            "run_id": run_id,
            "task_id": task_id,
            "decision": decision,
            "principal": principal,
            "scope": scope,
            "projection_hash": projection_hash,
            "journal_head": journal_head,
            "diff_hash": diff_hash,
            "journal_entry_hash": journal_entry_hash,
        },
    )


def record_run_ssh_task(
    *,
    chain: AuditChainStore,
    run_id: str,
    task_id: str,
    host: str,
    worktree: str,
    exit_code: int,
    worktree_digest: str,
    ledger_head: str,
    backend_name: str = "ssh",
    actor: str = "run_service_ssh",
) -> AuditEvent:
    """Append a ``run.ssh_task`` execution receipt into *chain* (#2352, AC4).

    Records one detached-run task executed on the ssh sandbox backend. The
    receipt binds the isolated remote worktree the task ran in and the
    work-ledger head at execution time, so an offline verifier can prove from
    the chain alone that each task of a goal ran in its own worktree across the
    ssh boundary. Only non-secret identifiers and content hashes are recorded --
    never goal text, task payloads, or the credentials injected into the remote
    environment (those flow through the credential vault, never the chain).

    Args:
        chain: The audit chain store accepting the entry.
        run_id: The detached run the task belongs to.
        task_id: The task that executed.
        host: The ssh host the task ran on (hostname only, never a secret).
        worktree: Absolute POSIX path of the isolated remote worktree.
        exit_code: The remote task command's exit code.
        worktree_digest: ``sha256:`` digest of the per-task isolation marker
            written into ``worktree`` (proves the marker landed in that tree).
        ledger_head: Work-ledger head entry hash at execution time.
        backend_name: The sandbox backend name; defaults to ``"ssh"``.
        actor: Recorded actor; defaults to ``"run_service_ssh"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded
        in its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_RUN_SSH_TASK,
        actor=actor,
        resource_type="run_ssh_task",
        resource_id=task_id,
        details={
            "run_id": run_id,
            "task_id": task_id,
            "backend": backend_name,
            "host": host,
            "worktree": worktree,
            "exit_code": exit_code,
            "worktree_digest": worktree_digest,
            "ledger_head": ledger_head,
        },
    )


#: Issue #2354 -- emitted whenever the live dispatch loop routes a task with
#: respect to the provider batch surface. The event mirrors the deterministic
#: :func:`~bernstein.core.cost.scheduling.batch.route_batch` decision so an
#: operator can prove, from the chain alone, that a batch-eligible task reached
#: the batch endpoint only on a batch-capable adapter -- and that an eligible
#: task on an adapter with no batch surface was refused (routed interactively),
#: never faked. Only the routing verdict and capability facts are recorded.
EVENT_COST_BATCH_ROUTE = "cost.batch_route"


def record_cost_batch_route(
    *,
    chain: AuditChainStore,
    run_id: str,
    task_id: str,
    adapter: str,
    batch_eligible: bool,
    adapter_capable: bool,
    capability: str,
    route: str,
    refused_reason: str,
    actor: str = "cost_policy",
) -> AuditEvent:
    """Append a ``cost.batch_route`` event into *chain* (#2354).

    Mirrors one live batch-routing decision into the HMAC chain so the routing
    of a task -- to the batch surface or to interactive dispatch -- is a
    verifiable receipt, not a log line. A batch-eligible task routes to
    ``batch`` only when the resolved adapter declares a batch surface; an
    eligible task on an incapable adapter routes ``interactive`` with
    *refused_reason* recorded, and a non-eligible task routes ``interactive``
    with no reason. A verifier reading the chain, the adapter capability map,
    and the task's eligibility recomputes the same verdict.

    Args:
        chain: The audit chain store accepting the entry.
        run_id: The run the task belonged to.
        task_id: The task being routed.
        adapter: Registry name of the resolved adapter.
        batch_eligible: Whether policy marked the task batch-eligible.
        adapter_capable: Whether the resolved adapter declares a batch surface.
        capability: The declared batch-dispatch capability string.
        route: ``batch`` or ``interactive``.
        refused_reason: Why an eligible task was refused the batch surface
            (empty unless a batch-eligible task hit an incapable adapter).
        actor: Recorded actor; defaults to ``"cost_policy"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded in
        its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_COST_BATCH_ROUTE,
        actor=actor,
        resource_type="cost_batch_route",
        resource_id=task_id,
        details={
            "run_id": run_id,
            "task_id": task_id,
            "adapter": adapter,
            "batch_eligible": batch_eligible,
            "adapter_capable": adapter_capable,
            "capability": capability,
            "route": route,
            "refused_reason": refused_reason,
        },
    )


def record_eval_gate_verdict(
    *,
    chain: AuditChainStore,
    receipt_hash: str,
    verdict: str,
    suite_content_hash: str,
    baseline_result_set_hash: str,
    candidate_result_set_hash: str,
    candidate_config_id: str,
    n_per_arm: int,
    effect: float,
    interval_low: float,
    interval_high: float,
    alpha: float,
    min_n_satisfied: bool,
    journal_entry_hash: str = "",
    actor: str = "eval_gate",
) -> AuditEvent:
    """Append an ``eval.gate_verdict`` event into *chain* (#2520).

    Mirrors one sealed statistical eval verdict receipt into the HMAC chain so
    an operator can prove, from the chain alone, that a promotion decision stood
    on a named body of statistical evidence: the paired suite it ran over, the
    two result sets it compared, the effect and its interval, and whether the
    minimum n per arm was met. The verdict is a pure function of that evidence,
    so a verifier holding the same result sets recomputes both the verdict and
    the ``receipt_hash`` byte-identically. Only hashes, the verdict, and the
    rounded statistics are recorded -- never task prompts or agent output.

    Args:
        chain: The audit chain store accepting the entry.
        receipt_hash: Content hash pinning the whole verdict receipt (its
            identity).
        verdict: One of ``significant_improvement`` / ``non_inferior`` /
            ``insufficient_evidence`` / ``significant_regression``.
        suite_content_hash: Order-invariant hash over the suite's task ids.
        baseline_result_set_hash: Order-invariant hash over the baseline arm's
            per-task pass/fail outcomes.
        candidate_result_set_hash: Order-invariant hash over the candidate arm's
            per-task pass/fail outcomes.
        candidate_config_id: Identifier of the candidate configuration under
            evaluation.
        n_per_arm: Paired sample size (equal per arm).
        effect: Candidate pass rate minus baseline pass rate (rounded).
        interval_low: Lower bound of the interval on the paired difference.
        interval_high: Upper bound of the interval on the paired difference.
        alpha: Significance level the verdict was decided at.
        min_n_satisfied: Whether the minimum n per arm was met.
        journal_entry_hash: Lineage-spine entry hash anchoring the sealed
            receipt bytes; a verifier holding the spine can recompute it.
        actor: Recorded actor; defaults to ``"eval_gate"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded in
        its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_EVAL_GATE_VERDICT,
        actor=actor,
        resource_type="eval_gate_verdict",
        resource_id=receipt_hash,
        details={
            "receipt_hash": receipt_hash,
            "verdict": verdict,
            "suite_content_hash": suite_content_hash,
            "baseline_result_set_hash": baseline_result_set_hash,
            "candidate_result_set_hash": candidate_result_set_hash,
            "candidate_config_id": candidate_config_id,
            "n_per_arm": n_per_arm,
            "effect": effect,
            "interval_low": interval_low,
            "interval_high": interval_high,
            "alpha": alpha,
            "min_n_satisfied": min_n_satisfied,
            "journal_entry_hash": journal_entry_hash,
        },
    )


def record_eval_gate_revocation(
    *,
    chain: AuditChainStore,
    receipt_hash: str,
    candidate_config_id: str,
    revoked_receipt_hashes: list[str],
    reverts_to_stage: str,
    reverts_to_config_id: str,
    trigger_receipt_hash: str,
    journal_entry_hash: str = "",
    actor: str = "eval_gate",
) -> AuditEvent:
    """Append an ``eval.gate_revocation`` event into *chain* (#2520).

    Mirrors a sealed revocation receipt into the HMAC chain when a
    significant_regression verdict rolls a candidate configuration back. The
    event names the verdict receipts the rollback revokes, the verdict receipt
    that triggered it, and the stage the deterministic promotion projection
    reverts to. A verifier folding the receipt chain offline reproduces the
    identical rollback, so a regression postmortem links the exact receipt that
    admitted the change to the receipt that revoked it.

    Args:
        chain: The audit chain store accepting the entry.
        receipt_hash: Content hash pinning the revocation receipt.
        candidate_config_id: The configuration being rolled back.
        revoked_receipt_hashes: Content hashes of the verdict receipts this
            revocation invalidates (the promoting receipts).
        reverts_to_stage: The stage the projection reverts to.
        reverts_to_config_id: The configuration that serves the reverted stage
            (the prior default).
        trigger_receipt_hash: The verdict receipt hash whose
            significant_regression verdict triggered the rollback.
        journal_entry_hash: Lineage-spine entry hash anchoring the sealed
            receipt bytes.
        actor: Recorded actor; defaults to ``"eval_gate"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded in
        its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_EVAL_GATE_REVOCATION,
        actor=actor,
        resource_type="eval_gate_revocation",
        resource_id=receipt_hash,
        details={
            "receipt_hash": receipt_hash,
            "candidate_config_id": candidate_config_id,
            "revoked_receipt_hashes": revoked_receipt_hashes.copy(),
            "reverts_to_stage": reverts_to_stage,
            "reverts_to_config_id": reverts_to_config_id,
            "trigger_receipt_hash": trigger_receipt_hash,
            "journal_entry_hash": journal_entry_hash,
        },
    )


def record_taint_decision(
    *,
    chain: AuditChainStore,
    target: str,
    trust: str,
    tainted: bool,
    decision: str,
    actor: str,
    closure_size: int = 0,
    trust_records: Sequence[str] = (),
) -> AuditEvent:
    """Append a ``provenance.taint_decision`` event into *chain* (issue #2513).

    Args:
        chain: The audit chain store accepting the entry.
        target: Entry hash / artefact whose propagated taint was consulted.
        trust: Effective trust class (``operator`` ... ``public``).
        tainted: Whether ``target`` was judged untrusted-origin.
        decision: The egress-relevant decision taken (e.g. ``deny``, ``ask``,
            ``approve``).
        actor: The subsystem or agent that made the decision.
        closure_size: Number of lineage entries in the projected closure.
        trust_records: Entry hashes of the signed provenance records the
            verdict projected from.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_PROVENANCE_TAINT_DECISION,
        actor=actor,
        resource_type="provenance_taint",
        resource_id=target,
        details={
            "target": target,
            "trust": trust,
            "tainted": tainted,
            "decision": decision,
            "closure_size": closure_size,
            "trust_records": list(trust_records),
        },
    )


def record_provenance_quarantine(
    *,
    chain: AuditChainStore,
    source_content_hash: str,
    extracted_fields: Sequence[str],
    withheld_fields: Sequence[str],
    actor: str,
) -> AuditEvent:
    """Append a ``provenance.quarantine`` event into *chain* (issue #2513).

    Anchors a quarantined structural extraction: which fields were kept and
    which free-text fields were withheld, tied to the source payload's content
    hash so the extraction edge is reconstructable offline.

    Args:
        chain: The audit chain store accepting the entry.
        source_content_hash: ``sha256:<hex>`` of the raw untrusted payload.
        extracted_fields: Names of the schema-validated fields emitted.
        withheld_fields: Names of the free-text fields withheld from context.
        actor: The ingestion point that ran the quarantine.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_PROVENANCE_QUARANTINE,
        actor=actor,
        resource_type="provenance_quarantine",
        resource_id=source_content_hash,
        details={
            "source_content_hash": source_content_hash,
            "extracted_fields": list(extracted_fields),
            "withheld_fields": list(withheld_fields),
        },
    )


#: Issue #2508 -- emitted for every fleet steering action (pause / resume /
#: guidance / redirect / abort) before its effect executes. The signed,
#: principal-named receipt binds the exact command payload the operator
#: confirmed (``payload_hash``), the target task, and the authorising scope
#: to a fixed chain position. The delivered effect references this event's
#: chain HMAC, so an effect with no matching receipt is rejected and a
#: mutated payload breaks verification at exactly this position -- a
#: tampered intervention is distinguishable from a steered one. The guidance
#: or redirect text itself never enters the chain (only its hash); it rides
#: the mailbox journal under DLP redaction. This constant is additive and
#: must never be reordered or removed.
EVENT_STEERING_RECEIPT = "steering.receipt"


def record_steering_receipt(
    *,
    chain: AuditChainStore,
    kind: str,
    task_id: str,
    principal: str,
    scope: str,
    payload_hash: str,
    actor: str = "fleet_steering",
) -> AuditEvent:
    """Append a ``steering.receipt`` event into *chain* (#2508).

    A fleet steering action (pause / resume / guidance / redirect / abort)
    is a receipt first and an effect second: the operator's exact command is
    bound into the HMAC chain here, before any effect executes, and the
    delivered effect references this receipt's chain HMAC. The effect is
    rejected when no matching receipt precedes it, so an operator
    intervention can never touch a worker without leaving a signed,
    position-fixed record. Mutating the recorded payload after the fact
    breaks the chain HMAC at exactly this position, so a tampered
    intervention is distinguishable from a legitimately steered one.

    Only the command's shape is recorded: the steering kind, the target
    task, the acting principal, the authorising scope, and the
    ``payload_hash`` -- the ``sha256:`` digest of the exact command payload
    the operator confirmed. The guidance or redirect text itself is never
    stored in the chain; it rides the mailbox journal under DLP redaction.

    Args:
        chain: The audit chain store accepting the entry.
        kind: One of ``pause`` / ``resume`` / ``guidance`` / ``redirect`` /
            ``abort``.
        task_id: The steered task.
        principal: The acting operator (seat attribution).
        scope: The authorising token scope the action was granted under.
        payload_hash: ``sha256:`` digest of the confirmed command payload;
            binds the receipt to the exact text shown in the confirmation UI.
        actor: Recorded actor; defaults to ``"fleet_steering"``.

    Returns:
        The recorded :class:`AuditEvent`; its ``hmac`` is the receipt
        identity the delivered effect references.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_STEERING_RECEIPT,
        actor=actor,
        resource_type="steering_command",
        resource_id=task_id,
        details={
            "kind": kind,
            "task_id": task_id,
            "principal": principal,
            "scope": scope,
            "payload_hash": payload_hash,
        },
    )


def record_mission_phase_receipt(
    *,
    chain: AuditChainStore,
    mission_id: str,
    phase_id: str,
    gate_passed: bool,
    receipt_hash: str,
    evidence_bundle_hashes: Sequence[str],
    ledger_seq: int,
    envelope: str,
    spend_usd: float,
    journal_entry_hash: str,
    reason: str = "",
    actor: str = "mission",
) -> AuditEvent:
    """Append a ``mission.phase_receipt`` event into *chain* (#2509).

    Mirrors a mission phase advancement (pass or envelope-exhausted halt) into
    the HMAC-chained audit log after the receipt was chained into the mission's
    work ledger. The receipt binds the gate verdict, the evidence bundle hashes
    it verified, the ledger position, the envelope, and the spend at gate time,
    so a phase pass is provable offline: a verifier recomputes the referenced
    evidence bundles and confirms the receipt is chain-attested. Only
    identifiers, hashes, the verdict, and the spend are recorded -- never goal
    text or task payloads.

    Args:
        chain: The audit chain store accepting the entry.
        mission_id: The mission the phase belongs to.
        phase_id: The advanced phase.
        gate_passed: Whether the verification gate passed (``False`` for a halt).
        receipt_hash: ``sha256:``/hex hash of the canonical receipt binding.
        evidence_bundle_hashes: Content addresses of the evidence bundles the
            gate verified.
        ledger_seq: The ledger position the receipt landed at.
        envelope: The phase's budget envelope name.
        spend_usd: The envelope spend at gate time.
        journal_entry_hash: The mission ledger ``entry_hash`` of the receipt row.
        reason: Halt reason (for example ``"envelope_exhausted"``); empty on a
            pass.
        actor: Recorded actor; defaults to ``"mission"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded in
        its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_MISSION_PHASE_RECEIPT,
        actor=actor,
        resource_type="mission_phase",
        resource_id=f"{mission_id}:{phase_id}",
        details={
            "mission_id": mission_id,
            "phase_id": phase_id,
            "gate_passed": gate_passed,
            "receipt_hash": receipt_hash,
            "evidence_bundle_hashes": list(evidence_bundle_hashes),
            "ledger_seq": ledger_seq,
            "envelope": envelope,
            "spend_usd": spend_usd,
            "reason": reason,
            "journal_entry_hash": journal_entry_hash,
        },
    )


def record_mission_digest_receipt(
    *,
    chain: AuditChainStore,
    mission_id: str,
    fire_time: int,
    digest_hash: str,
    receipt_id: str,
    mission_status_hash: str,
    ledger_head: str,
    phases_passed: int,
    gates_passed: int,
    gates_failed: int,
    total_spend_usd: float,
    schedule_id: str = "",
    recurrence: str = "",
    fire_graph_hash: str = "",
    journal_entry_hash: str = "",
    actor: str = "mission_digest",
) -> AuditEvent:
    """Append a ``mission.digest_receipt`` event into *chain* (#2510).

    Anchors one recurring mission-digest fire in the HMAC chain. The digest is
    a pure deterministic projection of the mission state at ``fire_time`` (see
    :mod:`bernstein.core.orchestration.mission_digest`); ``digest_hash`` is the
    hash of its canonical bytes, embedded in the posted chat message so a
    recipient recomputes the digest from the ledger and proves the message
    matches this chain-attested receipt. ``receipt_id`` is the per-fire delivery
    idempotency key, a pure function of ``(mission_id, fire_time, digest_hash)``.
    Only identifiers, hashes, counts, and the spend are recorded -- never goal
    text or task payloads.

    Args:
        chain: The audit chain store accepting the entry.
        mission_id: The mission the digest summarises.
        fire_time: Integer Unix epoch of the canonical fire instant.
        digest_hash: Hex hash of the canonical digest bytes.
        receipt_id: Deterministic per-fire delivery idempotency key.
        mission_status_hash: The mission projection's status hash at fire time.
        ledger_head: The work-ledger head the projection folded to.
        phases_passed: Count of phases the digest reports as passed.
        gates_passed: Count of phases whose verification gate passed.
        gates_failed: Count of phases halted (gate failed / envelope exhausted).
        total_spend_usd: Total spend across all envelopes at fire time.
        schedule_id: The recurring schedule that fired the digest, when any.
        recurrence: Canonical recurrence rule of the fire, when any.
        fire_graph_hash: The deterministic schedule-fire projection hash the
            digest fire was anchored on, when any.
        journal_entry_hash: The lineage-spine / ledger entry hash the digest
            was sealed against, when any.
        actor: Recorded actor; defaults to ``"mission_digest"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded in
        its details payload.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_MISSION_DIGEST_RECEIPT,
        actor=actor,
        resource_type="mission_digest",
        resource_id=receipt_id,
        details={
            "mission_id": mission_id,
            "fire_time": fire_time,
            "digest_hash": digest_hash,
            "receipt_id": receipt_id,
            "mission_status_hash": mission_status_hash,
            "ledger_head": ledger_head,
            "phases_passed": phases_passed,
            "gates_passed": gates_passed,
            "gates_failed": gates_failed,
            "total_spend_usd": total_spend_usd,
            "schedule_id": schedule_id,
            "recurrence": recurrence,
            "fire_graph_hash": fire_graph_hash,
            "journal_entry_hash": journal_entry_hash,
        },
    )


# ---------------------------------------------------------------------------
# Eventing v2 (#2548): fire receipts, automation actions, absence proofs, and
# webhook payload anchors. These are the chain records the events package writes
# so the unified feed reacts to itself: a rule fire mints a receipt before the
# effect runs, the executed action is its own chain event, an expired
# expectation carries a negative proof, and an inbound webhook payload is
# content-addressed into the chain before any template render.
# ---------------------------------------------------------------------------

EVENT_RULE_FIRE_RECEIPT = "events.rule_fire_receipt"
"""Rule fire receipt: rule hash, matched event hashes, and rendered action."""

EVENT_AUTOMATION_ACTION = "events.automation_action"
"""An executed automation action, referencing its fire receipt."""

EVENT_EXPECTATION_EXPIRED = "events.expectation_expired"
"""An expired absence expectation carrying a negative proof."""

EVENT_WEBHOOK_PAYLOAD_ANCHOR = "events.webhook_payload_anchor"
"""An inbound webhook payload content-addressed into the chain before render."""

EVENT_FEED_RENDER_FAILURE = "events.render_failure"
"""A webhook template render failure, carrying the payload digest only."""


def record_rule_fire_receipt(
    *,
    chain: AuditChainStore,
    rule_hash: str,
    matched_event_hmacs: Sequence[str],
    action_kind: str,
    action_digest: str,
    actor: str = "events_automation",
) -> AuditEvent:
    """Append an ``events.rule_fire_receipt`` event into *chain* (#2548).

    Minted before the effect executes, the receipt commits to the rule identity,
    the HMACs of the events that matched, and the rendered action's digest. A
    verifier holding the feed window can confirm the action a receipt authorises,
    and an executed action without a matching receipt is rejected.

    Args:
        chain: The audit chain store accepting the entry.
        rule_hash: ``sha256:`` identity of the rule that fired.
        matched_event_hmacs: HMACs of the events that satisfied the rule.
        action_kind: The rendered action's kind.
        action_digest: The rendered action's ``sha256:`` digest.
        actor: Recorded actor; defaults to ``"events_automation"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_RULE_FIRE_RECEIPT,
        actor=actor,
        resource_type="rule_fire_receipt",
        resource_id=action_digest,
        details={
            "rule_hash": rule_hash,
            "matched_event_hmacs": list(matched_event_hmacs),
            "action_kind": action_kind,
            "action_digest": action_digest,
        },
    )


def record_automation_action(
    *,
    chain: AuditChainStore,
    action_kind: str,
    action_digest: str,
    fire_receipt_hmac: str,
    triggering_event_hmac: str,
    result_status: str = "dispatched",
    actor: str = "events_automation",
) -> AuditEvent:
    """Append an ``events.automation_action`` event into *chain* (#2548).

    The executed action is itself a chain event, referencing the fire receipt
    that authorised it and the triggering event it was rendered against, so an
    automated intervention is observable in the same feed it reacts to.

    Args:
        chain: The audit chain store accepting the entry.
        action_kind: The executed action's kind.
        action_digest: The executed action's ``sha256:`` digest.
        fire_receipt_hmac: HMAC of the fire receipt event that authorised it.
        triggering_event_hmac: HMAC of the event the action was rendered against.
        result_status: Short status token for the effect outcome.
        actor: Recorded actor; defaults to ``"events_automation"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_AUTOMATION_ACTION,
        actor=actor,
        resource_type="automation_action",
        resource_id=action_digest,
        details={
            "action_kind": action_kind,
            "action_digest": action_digest,
            "fire_receipt_hmac": fire_receipt_hmac,
            "triggering_event_hmac": triggering_event_hmac,
            "result_status": result_status,
        },
    )


def record_expectation_expired(
    *,
    chain: AuditChainStore,
    after_hmac: str,
    to_hmac: str,
    expect: str,
    rule_hash: str = "",
    actor: str = "events_absence",
) -> AuditEvent:
    """Append an ``events.expectation_expired`` event into *chain* (#2548).

    The event embeds a negative proof: no event matching ``expect`` exists
    between the two named chain positions ``after_hmac`` and ``to_hmac``. A
    verifier confirms the assertion against the stored window; injecting a
    matching event into the window makes the proof fail.

    Args:
        chain: The audit chain store accepting the entry.
        after_hmac: HMAC of the anchoring event - the lower named position.
        to_hmac: HMAC of the last observed event - the upper named position.
        expect: The label glob asserted absent across the span.
        rule_hash: Optional ``sha256:`` identity of the expectation rule.
        actor: Recorded actor; defaults to ``"events_absence"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_EXPECTATION_EXPIRED,
        actor=actor,
        resource_type="expectation",
        resource_id=after_hmac,
        details={
            "after_hmac": after_hmac,
            "to_hmac": to_hmac,
            "expect": expect,
            "rule_hash": rule_hash,
        },
    )


def record_webhook_payload_anchor(
    *,
    chain: AuditChainStore,
    payload_digest: str,
    source: str,
    template_id: str,
    actor: str = "events_webhook",
) -> AuditEvent:
    """Append an ``events.webhook_payload_anchor`` event into *chain* (#2548).

    The raw inbound payload bytes are content-addressed into the chain before any
    template renders them, so a render is always reproducible from the recorded
    bytes. Only the digest is recorded - never the payload.

    Args:
        chain: The audit chain store accepting the entry.
        payload_digest: ``sha256:`` content address of the raw payload bytes.
        source: The inbound source label the payload arrived from.
        template_id: The template selected to render the payload.
        actor: Recorded actor; defaults to ``"events_webhook"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_WEBHOOK_PAYLOAD_ANCHOR,
        actor=actor,
        resource_type="webhook_payload",
        resource_id=payload_digest,
        details={
            "payload_digest": payload_digest,
            "source": source,
            "template_id": template_id,
        },
    )


def record_render_failure(
    *,
    chain: AuditChainStore,
    payload_digest: str,
    template_id: str,
    error_kind: str,
    source: str = "",
    actor: str = "events_webhook",
) -> AuditEvent:
    """Append an ``events.render_failure`` diagnostic event into *chain* (#2548).

    A template render failure surfaces as a feed event carrying the payload
    digest and a short error token, and never the payload content.

    Args:
        chain: The audit chain store accepting the entry.
        payload_digest: ``sha256:`` content address of the raw payload bytes.
        template_id: The template that failed to render.
        error_kind: Short token for the failure (``invalid_json`` /
            ``missing_resource``).
        source: The inbound source label, when known.
        actor: Recorded actor; defaults to ``"events_webhook"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_FEED_RENDER_FAILURE,
        actor=actor,
        resource_type="webhook_payload",
        resource_id=payload_digest,
        details={
            "payload_digest": payload_digest,
            "template_id": template_id,
            "error_kind": error_kind,
            "source": source,
        },
    )


# ---------------------------------------------------------------------------
# Registered-recipe lifecycle events (#2546)
# ---------------------------------------------------------------------------
#
# A registered recipe is a content-addressed run definition whose entire
# lifecycle lives on this chain: register, supersede (definition change),
# rollback (re-point the name at a prior hash), pause / resume (a
# definition-level fire gate), a collision receipt for every overlap the
# supervisor evaluates, and a fleet apply bound to a reviewed plan hash.
# There is no mutable registry row; the live name -> hash mapping is a pure
# projection of these receipts. The constants are additive; never edit or
# remove an existing entry.

#: A recipe definition was registered for the first time under a name.
EVENT_RECIPE_REGISTER = "recipe.register"

#: A changed definition body supersedes the prior hash for a name
#: (operator-signed: carries old_hash and new_hash and a spine edge).
EVENT_RECIPE_SUPERSEDE = "recipe.supersede"

#: A name was re-pointed at a prior definition hash (rollback receipt);
#: nothing is deleted, the roll-back is itself a chain record.
EVENT_RECIPE_ROLLBACK = "recipe.rollback"

#: A recipe was paused: a definition-level state record that stops future
#: fires while keeping the recipe's identity and history.
EVENT_RECIPE_PAUSE = "recipe.pause"

#: A paused recipe was resumed.
EVENT_RECIPE_RESUME = "recipe.resume"

#: The supervisor evaluated a concurrency collision against a still-running
#: fire and emitted a deterministic decision (ENQUEUE / CANCEL_NEW /
#: SUPERSEDE_WITH_HANDOFF). Emitted for every evaluated collision so a
#: double-fire is a recorded decision rather than a silent double-write.
EVENT_SCHEDULE_COLLISION = "schedule.collision_receipt"

#: A declarative fleet manifest was applied against an exact plan hash;
#: the apply receipt binds the reviewed plan to the registry mutation.
EVENT_RECIPE_FLEET_APPLY = "recipe.fleet_apply"

#: An operator resolved a forked definition lineage by naming which successor
#: of the contended predecessor the projection must follow. Nothing is
#: deleted: the losing branch stays on the chain and in ``history``, and the
#: resolution is itself an auditable receipt. This is the recovery path for a
#: fork produced by a concurrent write, which fails closed everywhere else.
EVENT_RECIPE_LINEAGE_RESOLVE = "recipe.lineage_resolve"

#: A registered recipe actually submitted work. Written only after the task
#: graph was handed to the dispatcher and the dispatcher returned identifiers
#: for work the sink accepted, so the presence of this receipt is evidence
#: that the fire happened; its absence means nothing was submitted. The
#: identifiers ride in the entry so the claim is checkable, not just signed.
EVENT_RECIPE_FIRE = "recipe.fire"

#: Issue #2518 -- emitted once per sovereign-profile activation. The active
#: residency posture (deny-all egress, offline catalog, local storage, strict
#: EU residency, compliance pack, and the config-derived declared endpoints /
#: catalogs) is projected into a canonical effective-policy document, signed
#: with the install's Ed25519 sovereign identity, and mirrored here by
#: recording ``{signed_body, signature, signer_public_key_pem}`` -- never the
#: operator's config file. The attestation, not the config file, is what an
#: auditor recomputes and checks against the chain.
EVENT_SOVEREIGN_ATTESTATION = "sovereign.posture_attestation"

#: Issue #2518 -- emitted once per spawn-time drift refusal. When the live
#: posture recomputed at spawn diverges from the attested posture hash, the
#: signed drift record naming the exact diverging keys is anchored here so the
#: divergence is a chain-attested refusal rather than a silent misconfiguration.
EVENT_SOVEREIGN_DRIFT = "sovereign.posture_drift"


def record_recipe_register(
    *,
    chain: AuditChainStore,
    name: str,
    recipe_hash: str,
    spine_anchor: str,
    prev_receipt_digest: str,
    actor: str = "recipe_registry",
) -> AuditEvent:
    """Append a ``recipe.register`` event into *chain* (#2546).

    Args:
        chain: The audit chain store accepting the entry.
        name: Operator-facing recipe name the hash is registered under.
        recipe_hash: ``sha256`` over the canonical definition bytes; the
            recipe's content-addressed identity.
        spine_anchor: Lineage-spine entry hash the canonical bytes were
            sealed into, or ``""`` when lineage recording is disabled.
        prev_receipt_digest: The chain digest of the previous lifecycle
            receipt for this name (``""`` for the first registration); the
            per-name definition lineage links through this field so a
            verifier can detect a reordered or missing receipt.
        actor: Recorded actor; defaults to ``"recipe_registry"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_RECIPE_REGISTER,
        actor=actor,
        resource_type="registered_recipe",
        resource_id=recipe_hash,
        details={
            "name": name,
            "recipe_hash": recipe_hash,
            "spine_anchor": spine_anchor,
            "prev_receipt_digest": prev_receipt_digest,
        },
    )


def record_recipe_supersede(
    *,
    chain: AuditChainStore,
    name: str,
    old_hash: str,
    new_hash: str,
    spine_anchor: str,
    prev_receipt_digest: str,
    actor: str = "recipe_registry",
) -> AuditEvent:
    """Append an operator-signed ``recipe.supersede`` event (#2546).

    The receipt names both the retired ``old_hash`` and the live
    ``new_hash`` so ``recipes history`` walks the definition change offline.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_RECIPE_SUPERSEDE,
        actor=actor,
        resource_type="registered_recipe",
        resource_id=new_hash,
        details={
            "name": name,
            "old_hash": old_hash,
            "new_hash": new_hash,
            "spine_anchor": spine_anchor,
            "prev_receipt_digest": prev_receipt_digest,
        },
    )


def record_recipe_rollback(
    *,
    chain: AuditChainStore,
    name: str,
    from_hash: str,
    to_hash: str,
    prev_receipt_digest: str,
    actor: str = "recipe_registry",
) -> AuditEvent:
    """Append a ``recipe.rollback`` event re-pointing *name* at a prior hash."""
    return chain.log_with_prev_digest(
        event_type=EVENT_RECIPE_ROLLBACK,
        actor=actor,
        resource_type="registered_recipe",
        resource_id=to_hash,
        details={
            "name": name,
            "from_hash": from_hash,
            "to_hash": to_hash,
            "prev_receipt_digest": prev_receipt_digest,
        },
    )


def record_recipe_pause(
    *,
    chain: AuditChainStore,
    name: str,
    recipe_hash: str,
    paused: bool,
    prev_receipt_digest: str,
    actor: str = "recipe_registry",
) -> AuditEvent:
    """Append a ``recipe.pause`` / ``recipe.resume`` state record (#2546).

    ``paused`` selects the event type so a single helper covers both
    transitions; the pause window is reconstructable from the receipts
    alone (a paused recipe keeps its identity and fires nothing).
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_RECIPE_PAUSE if paused else EVENT_RECIPE_RESUME,
        actor=actor,
        resource_type="registered_recipe",
        resource_id=recipe_hash,
        details={
            "name": name,
            "recipe_hash": recipe_hash,
            "paused": paused,
            "prev_receipt_digest": prev_receipt_digest,
        },
    )


def record_schedule_collision(
    *,
    chain: AuditChainStore,
    resource_id: str,
    policy: str,
    action: str,
    receipt_hash: str,
    running_fire_id: str,
    resume_from_checkpoint: str,
    warm_resume: bool,
    actor: str = "schedule_supervisor",
) -> AuditEvent:
    """Append a ``schedule.collision_receipt`` event (#2546).

    Emitted for every collision the supervisor evaluates, so an overrun is
    a recorded decision rather than a silent double-write. ``receipt_hash``
    is the stable hash of the pure :func:`decide_collision` outcome, so two
    operators over identical running-fire state record the same receipt.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_SCHEDULE_COLLISION,
        actor=actor,
        resource_type="schedule_collision",
        resource_id=resource_id,
        details={
            "policy": policy,
            "action": action,
            "receipt_hash": receipt_hash,
            "running_fire_id": running_fire_id,
            "resume_from_checkpoint": resume_from_checkpoint,
            "warm_resume": warm_resume,
        },
    )


def record_recipe_lineage_resolve(
    *,
    chain: AuditChainStore,
    name: str,
    predecessor: str,
    chosen_receipt: str,
    superseded_receipts: tuple[str, ...],
    actor: str = "operator",
) -> AuditEvent:
    """Append a ``recipe.lineage_resolve`` event fixing a forked lineage (#2654).

    A fork means one predecessor has two successors, so the projection cannot
    honestly pick a branch and fails closed. On an append-only chain the fork
    cannot be removed, so recovery is additive: the operator names the branch
    to follow, and that decision is recorded rather than applied silently.

    Args:
        chain: The audit chain store accepting the entry.
        name: Recipe name whose lineage is being resolved.
        predecessor: Receipt hmac that has more than one successor (``""``
            for a fork at genesis).
        chosen_receipt: Successor hmac the projection must follow.
        superseded_receipts: The other successors, recorded so the discarded
            branch stays visible.
        actor: Operator performing the resolution.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_RECIPE_LINEAGE_RESOLVE,
        actor=actor,
        resource_type="registered_recipe",
        resource_id=name,
        details={
            "name": name,
            "predecessor": predecessor,
            "chosen_receipt": chosen_receipt,
            "superseded_receipts": list(superseded_receipts),
        },
    )


def record_recipe_fire(
    *,
    chain: AuditChainStore,
    name: str,
    recipe_hash: str,
    fire_time: int,
    projection_hash: str,
    schedule_id: str,
    submitted_ids: tuple[str, ...],
    actor: str = "recipe_registry",
) -> AuditEvent:
    """Append a ``recipe.fire`` event for a fire that submitted work (#2654).

    The caller appends this only after the dispatcher returned identifiers for
    work the sink accepted, so the receipt is evidence that the fire ran
    rather than an assertion about it. A fire whose submission failed appends
    nothing and reports the failure to its caller instead.

    The identifiers are recorded, not merely counted: a reader of the chain
    can resolve each one and confirm the work exists, which is what separates
    an auditable receipt from a signed claim.

    Args:
        chain: The audit chain store accepting the entry.
        name: Operator-facing recipe name that fired.
        recipe_hash: The live content-addressed definition identity.
        fire_time: Unix epoch of the fire instant.
        projection_hash: Deterministic fire-projection hash.
        schedule_id: Content-derived id of the declared schedule that
            triggered the fire, or ``""`` for a schedule-neutral manual fire.
        submitted_ids: Identifiers of the work items the sink accepted.
        actor: Recorded actor; defaults to ``"recipe_registry"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_RECIPE_FIRE,
        actor=actor,
        resource_type="registered_recipe",
        resource_id=recipe_hash,
        details={
            "name": name,
            "recipe_hash": recipe_hash,
            "fire_time": fire_time,
            "projection_hash": projection_hash,
            "schedule_id": schedule_id,
            "submitted": len(submitted_ids),
            "submitted_ids": list(submitted_ids),
        },
    )


def record_recipe_fleet_apply(
    *,
    chain: AuditChainStore,
    plan_hash: str,
    applied: tuple[str, ...],
    actor: str = "recipe_registry",
) -> AuditEvent:
    """Append a ``recipe.fleet_apply`` event bound to the reviewed *plan_hash*."""
    return chain.log_with_prev_digest(
        event_type=EVENT_RECIPE_FLEET_APPLY,
        actor=actor,
        resource_type="recipe_fleet_plan",
        resource_id=plan_hash,
        details={
            "plan_hash": plan_hash,
            "applied": list(applied),
        },
    )


def record_audit_receipt_export(
    *,
    chain: AuditChainStore,
    head_sha256: str,
    since: str,
    until: str,
    event_count: int,
    receipt_sha256: str,
    formats: tuple[str, ...] | list[str],
    actor: str = "audit",
) -> AuditEvent:
    """Append an ``audit.receipt_export`` event into *chain* (#2604).

    Records that a standard, offline-verifiable receipt was projected over the
    chain range ``[since, until)`` and which head it bound. Only hashes, the
    window, the event count, and the emitted format list are recorded -- never
    event payloads.

    Args:
        chain: The audit chain store accepting the entry.
        head_sha256: The chain range head - the receipt's subject digest.
        since: ISO-8601 inclusive lower bound of the projected range.
        until: ISO-8601 exclusive upper bound of the projected range.
        event_count: Number of events in the projected range.
        receipt_sha256: SHA-256 of the canonical receipt bytes.
        formats: The receipt formats emitted (subset of cose/intoto/transparency).
        actor: Recorded actor; defaults to ``"audit"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_AUDIT_RECEIPT_EXPORT,
        actor=actor,
        resource_type="audit_receipt",
        resource_id=head_sha256,
        details={
            "head_sha256": head_sha256,
            "since": since,
            "until": until,
            "event_count": event_count,
            "receipt_sha256": receipt_sha256,
            "formats": sorted(formats),
        },
    )


#: Issue #2553 -- emitted once per agent-posted task artifact. The artifact
#: bytes are content-addressed in the evidence store and sealed into the lineage
#: spine; this mirror records only the identity (content hash, spine anchor,
#: journal position, version chain) so the fact that a worker posted a given
#: artifact at a known run position is itself chain-attested. The artifact
#: payload is never recorded here.
EVENT_RUN_ARTIFACT = "run.artifact"

#: Issue #2553 -- emitted when a caller is refused an artifact post against a
#: task whose claim it does not hold. The refusal is chain-attested so an
#: operator can prove, from the chain alone, that isolation held.
EVENT_RUN_ARTIFACT_REFUSED = "run.artifact_refused"


def record_run_artifact(
    *,
    chain: AuditChainStore,
    task_id: str,
    key: str,
    artifact_type: str,
    content_hash: str,
    version: int,
    prev_version_hash: str,
    spine_entry_hash: str,
    journal_index: int,
    journal_event_hash: str,
    actor: str = "run_artifact",
) -> AuditEvent:
    """Append a ``run.artifact`` event into *chain* (#2553).

    Mirrors an agent-posted task artifact into the HMAC-chained audit log. Only
    the artifact identity is recorded -- the content hash, its spine anchor, the
    version chain, and the anchoring journal position -- never the artifact
    bytes. A verifier holding the stored blob can recompute the content hash
    byte-identically and confirm the artifact is chain-attested.

    Args:
        chain: The audit chain store accepting the entry.
        task_id: The task the artifact is bound to.
        key: The artifact slot key.
        artifact_type: One of ``report`` / ``table`` / ``link``.
        content_hash: ``sha256:`` hash of the stored canonical bytes.
        version: 1-based version number within the key.
        prev_version_hash: The prior version's spine entry hash, or ``""``.
        spine_entry_hash: This version's lineage-spine entry hash (its identity).
        journal_index: 0-based index of the anchoring ``artifact_posted`` row.
        journal_event_hash: The anchoring journal row's Merkle ``event_hash``.
        actor: Recorded actor; defaults to ``"run_artifact"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_RUN_ARTIFACT,
        actor=actor,
        resource_type="run_artifact",
        resource_id=spine_entry_hash,
        details={
            "task_id": task_id,
            "key": key,
            "artifact_type": artifact_type,
            "content_hash": content_hash,
            "version": version,
            "prev_version_hash": prev_version_hash,
            "spine_entry_hash": spine_entry_hash,
            "journal_index": journal_index,
            "journal_event_hash": journal_event_hash,
        },
    )


def record_run_artifact_refused(
    *,
    chain: AuditChainStore,
    task_id: str,
    key: str,
    caller: str,
    reason: str,
    actor: str = "run_artifact",
) -> AuditEvent:
    """Append a ``run.artifact_refused`` event into *chain* (#2553).

    Records that a caller was refused an artifact post it was not authorised to
    make (it does not hold the target task's claim). The refusal is chain-
    attested so an operator can prove isolation held.

    Args:
        chain: The audit chain store accepting the entry.
        task_id: The task the post was refused against.
        key: The artifact key the caller attempted.
        caller: The identity that attempted the post.
        reason: A short typed reason string.
        actor: Recorded actor; defaults to ``"run_artifact"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_RUN_ARTIFACT_REFUSED,
        actor=actor,
        resource_type="run_artifact",
        resource_id=task_id,
        details={
            "task_id": task_id,
            "key": key,
            "caller": caller,
            "reason": reason,
        },
    )


# ---------------------------------------------------------------------------
# Fleet config plane events (#2550)
# ---------------------------------------------------------------------------
#
# Three named-configuration primitives share one discipline: every write,
# rotation, and activation is a chain event, so config identity is a pure
# projection of these receipts rather than a mutable live-state row. A
# variable write records the old and new value hashes plus the per-name
# write ordinal; a connection-document lifecycle records create / rotate /
# resolve / refuse against the document's content hash; a context activation
# records the canonical effective-settings hash. The constants are additive;
# never edit or remove an existing entry.

#: A fleet variable was written: carries the variable name, the prior value
#: hash, the new value hash, and the per-name write ordinal (chain_position).
#: A live-value config store cannot replay through mutation; because the
#: write hashes are chained, a later mutation cannot rewrite what a pinned
#: read resolved.
EVENT_FLEET_VAR_SET = "fleet.var_set"

#: A connection document was created for the first time under a name; the
#: document names a broker-managed secret and connector defaults but carries
#: no secret material, and is Ed25519-signed by the local install identity.
EVENT_FLEET_CONN_CREATE = "fleet.conn_create"

#: A connection document was rotated (old_hash -> new_hash); every consumer
#: that references the document by name re-points at the next mint with zero
#: task-spec edits. The rotation is itself a signed chain record.
EVENT_FLEET_CONN_ROTATE = "fleet.conn_rotate"

#: A connection document resolved through the broker mint path for a task;
#: the receipt binds document name, document hash, task id, and token id so
#: ``bernstein conn audit`` can reconstruct every resolving task offline.
EVENT_FLEET_CONN_RESOLVE = "fleet.conn_resolve"

#: A connection document refused to resolve (signature verification against
#: the local install identity failed, e.g. a document copied from another
#: install). The refusal is a recorded event, not a silent denial.
EVENT_FLEET_CONN_REFUSE = "fleet.conn_refuse"

#: A named operating context was activated: the receipt embeds the canonical
#: effective-settings hash so a run carries a configuration identity a
#: verifier can check, and replay can flag hash divergence with a named cause.
EVENT_FLEET_CONTEXT_ACTIVATE = "fleet.context_activate"


def record_fleet_var_set(
    *,
    chain: AuditChainStore,
    name: str,
    old_value_hash: str,
    new_value_hash: str,
    chain_position: int,
    actor: str = "fleet_config",
) -> AuditEvent:
    """Append a ``fleet.var_set`` event into *chain* (#2550).

    Args:
        chain: The audit chain store accepting the entry.
        name: The fleet variable name; the variable's identity is its chain
            segment (all ``fleet.var_set`` events sharing this ``resource_id``).
        old_value_hash: Content hash of the prior value (``""`` for the first
            write); pairs with ``new_value_hash`` so a mutated or deleted
            historical write flips ``verify`` with a named failing record.
        new_value_hash: Content hash of the value now in effect.
        chain_position: The per-name write ordinal (0 for the first write);
            a pinned read records this so divergence between two workers is
            explained by the writes landing between their two positions.
        actor: Recorded actor; defaults to ``"fleet_config"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_FLEET_VAR_SET,
        actor=actor,
        resource_type="fleet_variable",
        resource_id=name,
        details={
            "name": name,
            "old_value_hash": old_value_hash,
            "new_value_hash": new_value_hash,
            "chain_position": chain_position,
        },
    )


def record_fleet_conn_create(
    *,
    chain: AuditChainStore,
    name: str,
    document_hash: str,
    secret_name_digest: str,
    actor: str = "fleet_config",
) -> AuditEvent:
    """Append a ``fleet.conn_create`` event into *chain* (#2550).

    Args:
        chain: The audit chain store accepting the entry.
        name: Operator-facing connection document name.
        document_hash: ``sha256`` over the canonical unsigned document bytes;
            the document's content-addressed identity.
        secret_name_digest: Digest of the broker secret name the document
            references (never the raw secret name, never the secret value).
        actor: Recorded actor; defaults to ``"fleet_config"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_FLEET_CONN_CREATE,
        actor=actor,
        resource_type="fleet_connection",
        resource_id=name,
        details={
            "name": name,
            "document_hash": document_hash,
            "secret_name_digest": secret_name_digest,
        },
    )


def record_fleet_conn_rotate(
    *,
    chain: AuditChainStore,
    name: str,
    old_document_hash: str,
    new_document_hash: str,
    secret_name_digest: str,
    actor: str = "fleet_config",
) -> AuditEvent:
    """Append a ``fleet.conn_rotate`` event into *chain* (#2550).

    Args:
        chain: The audit chain store accepting the entry.
        name: Operator-facing connection document name.
        old_document_hash: Content hash of the superseded document version.
        new_document_hash: Content hash of the new document version.
        secret_name_digest: Digest of the (possibly new) broker secret name.
        actor: Recorded actor; defaults to ``"fleet_config"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_FLEET_CONN_ROTATE,
        actor=actor,
        resource_type="fleet_connection",
        resource_id=name,
        details={
            "name": name,
            "old_document_hash": old_document_hash,
            "new_document_hash": new_document_hash,
            "secret_name_digest": secret_name_digest,
        },
    )


def record_fleet_conn_resolve(
    *,
    chain: AuditChainStore,
    name: str,
    document_hash: str,
    task_id: str,
    token_id: str,
    actor: str = "fleet_config",
) -> AuditEvent:
    """Append a ``fleet.conn_resolve`` event into *chain* (#2550).

    The lineage receipt binds ``(document name, document hash, task id,
    token id)`` so a resolving task can be reconstructed offline from the
    chain alone. The raw secret never appears here.

    Args:
        chain: The audit chain store accepting the entry.
        name: Connection document name that resolved.
        document_hash: Content hash of the document version that resolved.
        task_id: The task the short-lived token was minted for.
        token_id: The broker-minted token identifier (opaque, non-secret).
        actor: Recorded actor; defaults to ``"fleet_config"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_FLEET_CONN_RESOLVE,
        actor=actor,
        resource_type="fleet_connection",
        resource_id=name,
        details={
            "name": name,
            "document_hash": document_hash,
            "task_id": task_id,
            "token_id": token_id,
        },
    )


def record_fleet_conn_refuse(
    *,
    chain: AuditChainStore,
    name: str,
    document_hash: str,
    reason: str,
    actor: str = "fleet_config",
) -> AuditEvent:
    """Append a ``fleet.conn_refuse`` event into *chain* (#2550).

    Args:
        chain: The audit chain store accepting the entry.
        name: Connection document name that refused to resolve.
        document_hash: Content hash of the refused document.
        reason: Machine-stable refusal reason (e.g.
            ``"signature_verification_failed"``).
        actor: Recorded actor; defaults to ``"fleet_config"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_FLEET_CONN_REFUSE,
        actor=actor,
        resource_type="fleet_connection",
        resource_id=name,
        details={
            "name": name,
            "document_hash": document_hash,
            "reason": reason,
        },
    )


def record_fleet_context_activate(
    *,
    chain: AuditChainStore,
    name: str,
    settings_hash: str,
    actor: str = "fleet_config",
) -> AuditEvent:
    """Append a ``fleet.context_activate`` event into *chain* (#2550).

    Args:
        chain: The audit chain store accepting the entry.
        name: The operating context name that was activated.
        settings_hash: Canonical effective-settings hash under the context;
            embedded in run receipts so config drift becomes a detected hash
            divergence with a named cause.
        actor: Recorded actor; defaults to ``"fleet_config"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_FLEET_CONTEXT_ACTIVATE,
        actor=actor,
        resource_type="fleet_context",
        resource_id=name,
        details={
            "name": name,
            "settings_hash": settings_hash,
        },
    )


# Cache policy engine (#2551): every hit, miss, dedup claim, and eviction is an
# audit-chain event carrying the policy hash and recipe hash, so the fact a
# cache decision was taken under a named policy is itself chain-attested. Only
# hashes, identifiers, and the decision are recorded -- never prompt or output
# payloads.
# ---------------------------------------------------------------------------

#: A policy-gated cache lookup hit a fresh, admissible entry. Records the
#: composed key, the policy and recipe hashes, the served entry's content id,
#: and whether the served output was verified.
EVENT_CACHE_HIT = "cache.hit"

#: A policy-gated cache lookup found no admissible entry (absent, stale, or
#: tombstoned). Records the composed key, the policy and recipe hashes, and a
#: machine-readable miss reason.
EVENT_CACHE_MISS = "cache.miss"

#: A fleet worker deduped onto another worker's in-flight spawn for the same
#: cache key. Records the key, the winner, the loser, the claim position, and
#: the duplicate-of receipt tag so the dedup is receipt-verified, not trusted.
EVENT_CACHE_DEDUP_CLAIM = "cache.dedup_claim"

#: A cache key (and everything reachable over served-from edges) was evicted.
#: Records the root key, the reason, the tombstoned count, and the recall set
#: size so the revocation and its blast radius are chain-attested.
EVENT_CACHE_EVICTION = "cache.eviction"


def record_cache_hit(
    *,
    chain: AuditChainStore,
    cache_key: str,
    policy_hash: str,
    recipe_hash: str,
    entry_content_id: str,
    verified: bool,
    actor: str = "cache_policy",
) -> AuditEvent:
    """Append a ``cache.hit`` event into *chain* (#2551).

    Args:
        chain: The audit chain store accepting the entry.
        cache_key: The composed cache key (hex).
        policy_hash: ``sha256:`` hash of the policy in force.
        recipe_hash: ``sha256:`` hash of the composed recipe.
        entry_content_id: ``sha256:`` content id of the served cache entry.
        verified: Whether the served output passed the completion gate.
        actor: Recorded actor; defaults to ``"cache_policy"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_CACHE_HIT,
        actor=actor,
        resource_type="cache_entry",
        resource_id=cache_key,
        details={
            "cache_key": cache_key,
            "policy_hash": policy_hash,
            "recipe_hash": recipe_hash,
            "entry_content_id": entry_content_id,
            "verified": verified,
        },
    )


def record_cache_miss(
    *,
    chain: AuditChainStore,
    cache_key: str,
    policy_hash: str,
    recipe_hash: str,
    reason: str,
    actor: str = "cache_policy",
) -> AuditEvent:
    """Append a ``cache.miss`` event into *chain* (#2551).

    Args:
        chain: The audit chain store accepting the entry.
        cache_key: The composed cache key (hex).
        policy_hash: ``sha256:`` hash of the policy in force.
        recipe_hash: ``sha256:`` hash of the composed recipe.
        reason: Machine-readable miss reason (``absent`` / ``stale`` /
            ``tombstoned`` / ``unverified``).
        actor: Recorded actor; defaults to ``"cache_policy"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_CACHE_MISS,
        actor=actor,
        resource_type="cache_entry",
        resource_id=cache_key,
        details={
            "cache_key": cache_key,
            "policy_hash": policy_hash,
            "recipe_hash": recipe_hash,
            "reason": reason,
        },
    )


def record_cache_dedup_claim(
    *,
    chain: AuditChainStore,
    cache_key: str,
    winner: str,
    loser: str,
    claim_position: int,
    receipt_hmac: str,
    policy_hash: str,
    recipe_hash: str,
    actor: str = "cache_policy",
) -> AuditEvent:
    """Append a ``cache.dedup_claim`` event into *chain* (#2551).

    Args:
        chain: The audit chain store accepting the entry.
        cache_key: The contended cache key (hex).
        winner: Claimer id that won the spawn.
        loser: Claimer id that deduped.
        claim_position: 1-based arrival order of the loser.
        receipt_hmac: The duplicate-of receipt tag proving the dedup edge.
        policy_hash: ``sha256:`` hash of the policy in force.
        recipe_hash: ``sha256:`` hash of the composed recipe.
        actor: Recorded actor; defaults to ``"cache_policy"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_CACHE_DEDUP_CLAIM,
        actor=actor,
        resource_type="cache_dedup",
        resource_id=cache_key,
        details={
            "cache_key": cache_key,
            "winner": winner,
            "loser": loser,
            "claim_position": claim_position,
            "receipt_hmac": receipt_hmac,
            "policy_hash": policy_hash,
            "recipe_hash": recipe_hash,
        },
    )


def record_cache_eviction(
    *,
    chain: AuditChainStore,
    cache_key: str,
    reason: str,
    tombstoned_count: int,
    recall_count: int,
    actor: str = "cache_policy",
) -> AuditEvent:
    """Append a ``cache.eviction`` event into *chain* (#2551).

    Args:
        chain: The audit chain store accepting the entry.
        cache_key: The evicted root key (hex).
        reason: The operator-supplied revocation reason.
        tombstoned_count: Number of keys tombstoned by this eviction.
        recall_count: Number of consuming runs in the recall set.
        actor: Recorded actor; defaults to ``"cache_policy"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_CACHE_EVICTION,
        actor=actor,
        resource_type="cache_entry",
        resource_id=cache_key,
        details={
            "cache_key": cache_key,
            "reason": reason,
            "tombstoned_count": tombstoned_count,
            "recall_count": recall_count,
        },
    )


def record_sovereign_attestation(
    *,
    chain: AuditChainStore,
    profile: str,
    posture_hash: str,
    signed_body: dict[str, Any],
    signature: str,
    signer_public_key_pem: str,
    actor: str = "sovereign_profile",
) -> AuditEvent:
    """Anchor a signed sovereign posture attestation into *chain* (#2518).

    Mirrors a sealed posture attestation into the HMAC-chained audit log so an
    auditor can prove, from the chain alone, that a residency posture was
    activated and signed. The signed body (the canonical effective-policy
    document, its posture hash, and the timestamp), the detached Ed25519
    signature, and the embedded public key are recorded so the record
    re-verifies offline and key-material free -- never the operator's raw config
    file.

    Args:
        chain: The audit chain store accepting the entry.
        profile: The activated profile name (``sovereign``).
        posture_hash: ``sha256:`` hash of the canonical effective-policy
            document; the posture identity and this record's subject.
        signed_body: The canonical signed preimage (effective-policy document,
            posture hash, schema version, timestamp).
        signature: Base64url-encoded detached Ed25519 signature over the
            canonical ``signed_body`` bytes.
        signer_public_key_pem: PEM public key that verifies ``signature``.
        actor: Recorded actor; defaults to ``"sovereign_profile"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_SOVEREIGN_ATTESTATION,
        actor=actor,
        resource_type="sovereign_posture",
        resource_id=posture_hash,
        details={
            "profile": profile,
            "posture_hash": posture_hash,
            "signed_body": signed_body,
            "signature": signature,
            "signer_public_key_pem": signer_public_key_pem,
        },
    )


def record_sovereign_drift(
    *,
    chain: AuditChainStore,
    profile: str,
    observed_hash: str,
    signed_body: dict[str, Any],
    signature: str,
    signer_public_key_pem: str,
    actor: str = "sovereign_profile",
) -> AuditEvent:
    """Anchor a signed sovereign posture drift refusal into *chain* (#2518).

    Emitted when the live posture recomputed at spawn diverges from the
    attested posture. The signed body names the attested and observed hashes
    and the exact diverging keys, so the divergence is a chain-attested,
    offline-verifiable refusal rather than a silent misconfiguration.

    Args:
        chain: The audit chain store accepting the entry.
        profile: The active profile name (``sovereign``).
        observed_hash: ``sha256:`` hash of the recomputed live posture; this
            record's subject.
        signed_body: The canonical signed preimage (attested + observed hashes,
            diverging keys, the observed effective-policy document, timestamp).
        signature: Base64url-encoded detached Ed25519 signature over the
            canonical ``signed_body`` bytes.
        signer_public_key_pem: PEM public key that verifies ``signature``.
        actor: Recorded actor; defaults to ``"sovereign_profile"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_SOVEREIGN_DRIFT,
        actor=actor,
        resource_type="sovereign_posture_drift",
        resource_id=observed_hash,
        details={
            "profile": profile,
            "observed_hash": observed_hash,
            "signed_body": signed_body,
            "signature": signature,
            "signer_public_key_pem": signer_public_key_pem,
        },
    )


# ---------------------------------------------------------------------------
# Named sandbox pools (#2547)
# ---------------------------------------------------------------------------
# A pool's lifecycle lives in the chain: register/update/retire events are the
# only mutation path, and the runtime pool registry is a deterministic
# projection rebuilt by replaying them (see
# ``bernstein.core.sandbox.pool_registry``). Placement is a receipt: every
# dispatch seals ``(pool_hash, effective_manifest_hash, chosen_backend)`` so a
# run's placement is provable offline from the chain alone, and a refused
# override is itself chained so an over-broad recipe leaves a tamper-evident
# trace instead of merely being absent from logs.

#: A pool manifest was registered. Records the pool name and its canonical
#: ``pool_hash`` so the registry projection can be rebuilt from the chain.
EVENT_POOL_REGISTERED = "pool.registered"

#: A pool manifest was updated (superseded by a new canonical hash). Records
#: both the prior and the new ``pool_hash`` so the projection is unambiguous.
EVENT_POOL_UPDATED = "pool.updated"

#: A pool was retired. The projection drops it; existing receipts still verify.
EVENT_POOL_RETIRED = "pool.retired"

#: A governed override was refused by the pool ceiling before any sandbox was
#: created. Records the pool, the offending field, and the machine-readable
#: refusal reason so the fail-closed decision is chain-attested, not just a log.
EVENT_POOL_OVERRIDE_REFUSED = "pool.override_refused"

#: A dispatch sealed its placement into the chain. Records the pool, template,
#: overrides, and effective manifest hashes plus the chosen backend, so the
#: placement of any run is provable offline (AC: verifiability).
EVENT_POOL_PLACEMENT_RECEIPT = "pool.placement_receipt"

#: A worker enrolled into a pool. Records the ``pool_hash``, the worker's
#: install-identity key id, and the enrolment signature so the execution host
#: of subsequent claims is cryptographically attributable (AC: verifiability).
EVENT_POOL_WORKER_ENROLLED = "pool.worker_enrolled"

#: A pool-scoped claim was signed by an enrolled worker. Records the claim
#: hash, the worker key id, the signature, and the placement receipt hash so a
#: reviewer can prove which enrolled host executed the run.
EVENT_POOL_CLAIM_RECEIPT = "pool.claim_receipt"

#: A warm pre-provisioned slot was quarantined because its provisioned manifest
#: hash diverged from the dispatch's effective hash. Records both hashes so the
#: infra drift is chain-attested and the dispatch falls back to cold cleanly.
EVENT_POOL_WARM_QUARANTINE = "pool.warm_quarantine"

#: Issue #2611 -- emitted once per attenuated delegation capability-token mint
#: (see :mod:`bernstein.core.security.capability_tokens`). The event mirrors the
#: token's ``{token_hash, issuer_identity_id, subject_identity_id,
#: parent_token_hash, remaining_depth}`` into the HMAC chain and embeds the prior
#: chain digest, so the token's ``audit_head`` (the tip captured at mint) and the
#: event's ``prev_chain_digest`` cross-reference each other. A verifier can prove,
#: from the chain alone, that a specific authority token was minted at a specific
#: point in history without the record exposing the token's caveats or signature.
EVENT_DELEGATION_MINTED = "delegation_minted"


def record_delegation_minted(
    *,
    chain: AuditChainStore,
    token_hash: str,
    issuer_identity_id: str,
    subject_identity_id: str,
    parent_token_hash: str,
    remaining_depth: int,
    actor: str = "delegation_minter",
) -> AuditEvent:
    """Append a ``delegation_minted`` event into *chain* (#2611).

    Anchors one attenuated capability-token mint in the HMAC chain. The embedded
    ``prev_chain_digest`` equals the tip the token captured as its ``audit_head``,
    and ``token_hash`` is the JCS hash of the token body, so the chain and the
    token cross-reference each other: a verifier holding both can prove the token
    was minted at this exact chain position. Only identifiers, the parent hash,
    and the remaining delegation depth are recorded -- never caveats or key
    material.

    Args:
        chain: The audit chain store accepting the entry.
        token_hash: JCS hash of the minted token body (its identity).
        issuer_identity_id: Identity that issued (signed) the token.
        subject_identity_id: Identity the token was delegated to.
        parent_token_hash: Hash of the parent token (genesis for a root mint).
        remaining_depth: The token's ``max_depth`` caveat after this hop.
        actor: Recorded actor; defaults to ``"delegation_minter"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_DELEGATION_MINTED,
        actor=actor,
        resource_type="capability_token",
        resource_id=token_hash,
        details={
            "token_hash": token_hash,
            "issuer_identity_id": issuer_identity_id,
            "subject_identity_id": subject_identity_id,
            "parent_token_hash": parent_token_hash,
            "remaining_depth": remaining_depth,
        },
    )


def record_pool_registered(
    *,
    chain: AuditChainStore,
    pool_name: str,
    pool_hash: str,
    actor: str = "operator",
) -> AuditEvent:
    """Append a ``pool.registered`` event into *chain* (#2547).

    Args:
        chain: The audit chain store accepting the entry.
        pool_name: Operator-facing pool name.
        pool_hash: Canonical hash identifying the registered pool manifest.
        actor: Recorded actor; defaults to ``"operator"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_POOL_REGISTERED,
        actor=actor,
        resource_type="sandbox_pool",
        resource_id=pool_hash,
        details={"pool_name": pool_name, "pool_hash": pool_hash},
    )


def record_pool_updated(
    *,
    chain: AuditChainStore,
    pool_name: str,
    pool_hash: str,
    prev_pool_hash: str,
    actor: str = "operator",
) -> AuditEvent:
    """Append a ``pool.updated`` event into *chain* (#2547).

    Args:
        chain: The audit chain store accepting the entry.
        pool_name: Operator-facing pool name.
        pool_hash: New canonical hash identifying the pool manifest.
        prev_pool_hash: The superseded pool hash for the same name.
        actor: Recorded actor; defaults to ``"operator"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_POOL_UPDATED,
        actor=actor,
        resource_type="sandbox_pool",
        resource_id=pool_hash,
        details={"pool_name": pool_name, "pool_hash": pool_hash, "prev_pool_hash": prev_pool_hash},
    )


def record_pool_retired(
    *,
    chain: AuditChainStore,
    pool_name: str,
    pool_hash: str,
    actor: str = "operator",
) -> AuditEvent:
    """Append a ``pool.retired`` event into *chain* (#2547).

    Args:
        chain: The audit chain store accepting the entry.
        pool_name: Operator-facing pool name.
        pool_hash: Canonical hash of the retired pool manifest.
        actor: Recorded actor; defaults to ``"operator"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_POOL_RETIRED,
        actor=actor,
        resource_type="sandbox_pool",
        resource_id=pool_hash,
        details={"pool_name": pool_name, "pool_hash": pool_hash},
    )


def record_pool_override_refused(
    *,
    chain: AuditChainStore,
    pool_hash: str,
    reason: str,
    refused_field: str,
    overrides_hash: str,
    author: str = "recipe",
    actor: str = "sandbox_pool",
) -> AuditEvent:
    """Append a ``pool.override_refused`` event into *chain* (#2547).

    The refusal itself is a chained receipt: an override that widened egress,
    added a credential env var beyond the ceiling, or touched a non-exposed
    field leaves a tamper-evident trace and no sandbox is created.

    Args:
        chain: The audit chain store accepting the entry.
        pool_hash: Canonical hash of the pool that refused the override.
        reason: Machine-readable refusal reason (e.g. ``egress_widened``).
        refused_field: The offending override field.
        overrides_hash: Hash of the canonical overrides that were refused.
        author: Who authored the override (``"recipe"`` or ``"agent"``), so a
            refusal driven by an agent-authored recipe is distinguishable.
        actor: Recorded actor; defaults to ``"sandbox_pool"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_POOL_OVERRIDE_REFUSED,
        actor=actor,
        resource_type="sandbox_pool",
        resource_id=pool_hash,
        details={
            "pool_hash": pool_hash,
            "reason": reason,
            "refused_field": refused_field,
            "overrides_hash": overrides_hash,
            "author": author,
        },
    )


def record_pool_placement_receipt(
    *,
    chain: AuditChainStore,
    placement_hash: str,
    pool_hash: str,
    template_hash: str,
    overrides_hash: str,
    effective_manifest_hash: str,
    chosen_backend: str,
    selector_inputs_hash: str,
    actor: str = "sandbox_pool",
) -> AuditEvent:
    """Append a ``pool.placement_receipt`` event into *chain* (#2547).

    Mirrors one sealed placement into the HMAC chain so the placement of any
    run is provable offline. A verifier holding the same pool manifest and
    recipe recomputes ``effective_manifest_hash`` byte-identically; flipping one
    byte of the recorded effective manifest breaks chain verification.

    Args:
        chain: The audit chain store accepting the entry.
        placement_hash: Self-hash pinning the whole placement receipt.
        pool_hash: The pool the placement targeted.
        template_hash: Hash of the pool base template.
        overrides_hash: Hash of the canonical overrides.
        effective_manifest_hash: Hash pinning the effective manifest.
        chosen_backend: The backend the pure selector chose.
        selector_inputs_hash: Hash over the selector inputs the pick used.
        actor: Recorded actor; defaults to ``"sandbox_pool"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_POOL_PLACEMENT_RECEIPT,
        actor=actor,
        resource_type="sandbox_placement",
        resource_id=placement_hash,
        details={
            "placement_hash": placement_hash,
            "pool_hash": pool_hash,
            "template_hash": template_hash,
            "overrides_hash": overrides_hash,
            "effective_manifest_hash": effective_manifest_hash,
            "chosen_backend": chosen_backend,
            "selector_inputs_hash": selector_inputs_hash,
        },
    )


def record_pool_worker_enrolled(
    *,
    chain: AuditChainStore,
    pool_hash: str,
    worker_name: str,
    keyid: str,
    enrolment_hash: str,
    signature: str,
    actor: str = "task_server",
) -> AuditEvent:
    """Append a ``pool.worker_enrolled`` event into *chain* (#2547).

    Binds a worker's Ed25519 install-identity key id to a ``pool_hash`` so the
    execution host of every subsequent claim is cryptographically attributable
    and ``bernstein audit verify`` can prove it offline.

    Args:
        chain: The audit chain store accepting the entry.
        pool_hash: The pool the worker enrolled into.
        worker_name: The worker's declared name.
        keyid: The worker install-identity key thumbprint (RFC 7638).
        enrolment_hash: Self-hash of the signed enrolment receipt body.
        signature: Base64 Ed25519 signature over the enrolment body.
        actor: Recorded actor; defaults to ``"task_server"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_POOL_WORKER_ENROLLED,
        actor=actor,
        resource_type="pool_worker",
        resource_id=keyid,
        details={
            "pool_hash": pool_hash,
            "worker_name": worker_name,
            "keyid": keyid,
            "enrolment_hash": enrolment_hash,
            "signature": signature,
        },
    )


def record_pool_claim_receipt(
    *,
    chain: AuditChainStore,
    claim_hash: str,
    pool_hash: str,
    task_id: str,
    keyid: str,
    signature: str,
    placement_hash: str,
    actor: str = "pool_worker",
) -> AuditEvent:
    """Append a ``pool.claim_receipt`` event into *chain* (#2547).

    Args:
        chain: The audit chain store accepting the entry.
        claim_hash: Self-hash of the signed claim receipt body.
        pool_hash: The pool the claim executed under.
        task_id: The claimed task id.
        keyid: The enrolled worker's install-identity key thumbprint.
        signature: Base64 Ed25519 signature over the claim body.
        placement_hash: The placement receipt hash the completion carries.
        actor: Recorded actor; defaults to ``"pool_worker"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_POOL_CLAIM_RECEIPT,
        actor=actor,
        resource_type="pool_claim",
        resource_id=claim_hash,
        details={
            "claim_hash": claim_hash,
            "pool_hash": pool_hash,
            "task_id": task_id,
            "keyid": keyid,
            "signature": signature,
            "placement_hash": placement_hash,
        },
    )


def record_pool_warm_quarantine(
    *,
    chain: AuditChainStore,
    pool_hash: str,
    provisioned_manifest_hash: str,
    dispatch_manifest_hash: str,
    slot_id: str,
    actor: str = "sandbox_pool",
) -> AuditEvent:
    """Append a ``pool.warm_quarantine`` event into *chain* (#2547).

    Args:
        chain: The audit chain store accepting the entry.
        pool_hash: The pool the warm slot belonged to.
        provisioned_manifest_hash: Effective hash the slot was provisioned for.
        dispatch_manifest_hash: Effective hash the dispatch required.
        slot_id: Identifier of the quarantined warm slot.
        actor: Recorded actor; defaults to ``"sandbox_pool"``.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded.
    """
    return chain.log_with_prev_digest(
        event_type=EVENT_POOL_WARM_QUARANTINE,
        actor=actor,
        resource_type="warm_slot",
        resource_id=slot_id,
        details={
            "pool_hash": pool_hash,
            "provisioned_manifest_hash": provisioned_manifest_hash,
            "dispatch_manifest_hash": dispatch_manifest_hash,
            "slot_id": slot_id,
        },
    )


__all__ = [
    "AGENT_FRESH_RESTART_ON_RETRY",
    "EVENT_A2A_MESSAGE_RECEIPT",
    "EVENT_ACTIVITY_RESULT",
    "EVENT_ADAPTER_ADMISSION_RECEIPT",
    "EVENT_ADAPTER_CANARY_RECEIPT",
    "EVENT_ADAPTER_CAPABILITY_REFUSAL",
    "EVENT_ADAPTER_CAPABILITY_SELECTION",
    "EVENT_ADAPTER_FLOOR_UPDATE",
    "EVENT_ADAPTER_SPAWN_PREFLIGHT",
    "EVENT_ADAPTER_VERSION_POSTURE",
    "EVENT_APPROVAL_CARD_ISSUED",
    "EVENT_APPROVAL_CARD_REFUSED",
    "EVENT_APPROVAL_CARD_RESOLVED",
    "EVENT_AUDIT_RECEIPT_EXPORT",
    "EVENT_AUTOMATION_ACTION",
    "EVENT_CACHE_DEDUP_CLAIM",
    "EVENT_CACHE_EVICTION",
    "EVENT_CACHE_HIT",
    "EVENT_CACHE_MISS",
    "EVENT_CHECKPOINT_RETRY",
    "EVENT_CLAIM_JOURNAL_RECEIPT",
    "EVENT_COMPACTION_RECEIPT",
    "EVENT_COMPACTION_SENSITIVE_GATE",
    "EVENT_COMPUTER_USE_ACTION",
    "EVENT_CONTEXT_CAPSULE",
    "EVENT_COST_BATCH_ROUTE",
    "EVENT_COST_DISPATCH_RECEIPT",
    "EVENT_COST_PROFILE_REPORT",
    "EVENT_DASHBOARD_TOKEN_GRANT",
    "EVENT_DELEGATION_MINTED",
    "EVENT_ENDPOINT_CERTIFICATION",
    "EVENT_ESCALATION_RECEIPT",
    "EVENT_EVAL_AB_COMPARISON",
    "EVENT_EVAL_GATE_REVOCATION",
    "EVENT_EVAL_GATE_VERDICT",
    "EVENT_EVIDENCE_BUNDLE",
    "EVENT_EXPECTATION_EXPIRED",
    "EVENT_FEED_RENDER_FAILURE",
    "EVENT_FLEET_CONN_CREATE",
    "EVENT_FLEET_CONN_REFUSE",
    "EVENT_FLEET_CONN_RESOLVE",
    "EVENT_FLEET_CONN_ROTATE",
    "EVENT_FLEET_CONTEXT_ACTIVATE",
    "EVENT_FLEET_VAR_SET",
    "EVENT_FORK_SNAPSHOT",
    "EVENT_GATE_ADJUDICATION",
    "EVENT_GOVERNANCE_DECISION",
    "EVENT_INPUT_REFUSAL",
    "EVENT_INTENT_CAPSULE",
    "EVENT_INTENT_DRIFT",
    "EVENT_MANDATE_CONSENT_RECEIPT",
    "EVENT_MANDATE_REVOCATION",
    "EVENT_MCP_STATELESS_CALL",
    "EVENT_MCP_TASK_HANDLE",
    "EVENT_MEMORY_WRITE",
    "EVENT_MISSION_DIGEST_RECEIPT",
    "EVENT_MISSION_PHASE_RECEIPT",
    "EVENT_MULTIMODAL_ATTACH",
    "EVENT_ODATA_WRITEBACK",
    "EVENT_OTEL_PROJECTION",
    "EVENT_PAYMENT_AUTHORIZED",
    "EVENT_PAYMENT_REFUSED",
    "EVENT_PLUGIN_CONFORMANCE_RECEIPT",
    "EVENT_PLUGIN_INSTALL_RECEIPT",
    "EVENT_PLUGIN_UPDATE_RECEIPT",
    "EVENT_POOL_CLAIM_RECEIPT",
    "EVENT_POOL_OVERRIDE_REFUSED",
    "EVENT_POOL_PLACEMENT_RECEIPT",
    "EVENT_POOL_REGISTERED",
    "EVENT_POOL_RETIRED",
    "EVENT_POOL_UPDATED",
    "EVENT_POOL_WARM_QUARANTINE",
    "EVENT_POOL_WORKER_ENROLLED",
    "EVENT_PROCESS_REAP_RECEIPT",
    "EVENT_PROVENANCE_QUARANTINE",
    "EVENT_PROVENANCE_TAINT_DECISION",
    "EVENT_PROVIDER_STATE_MUTATION",
    "EVENT_RECIPE_FIRE",
    "EVENT_RECIPE_FLEET_APPLY",
    "EVENT_RECIPE_LINEAGE_RESOLVE",
    "EVENT_RECIPE_PAUSE",
    "EVENT_RECIPE_REGISTER",
    "EVENT_RECIPE_RESUME",
    "EVENT_RECIPE_ROLLBACK",
    "EVENT_RECIPE_SUPERSEDE",
    "EVENT_REVIEW_BOARD_ACTION",
    "EVENT_REVIEW_RECEIPT",
    "EVENT_ROUTING_FAILOVER_RECEIPT",
    "EVENT_RULE_FIRE_RECEIPT",
    "EVENT_RUN_ARTIFACT",
    "EVENT_RUN_ARTIFACT_REFUSED",
    "EVENT_RUN_LIFECYCLE",
    "EVENT_RUN_SSH_TASK",
    "EVENT_SCHEDULE_COLLISION",
    "EVENT_SCHEDULE_FIRE_PROJECTION",
    "EVENT_SIGNAL_GATE_PROJECTION",
    "EVENT_SKILL_INSTALL_RECEIPT",
    "EVENT_SKILL_USAGE",
    "EVENT_SKILL_VERIFICATION_REFUSAL",
    "EVENT_SLA_VIOLATION",
    "EVENT_SOVEREIGN_ATTESTATION",
    "EVENT_SOVEREIGN_DRIFT",
    "EVENT_SPEC_REQUIREMENT_SET",
    "EVENT_SPIFFE_SVID_BINDING",
    "EVENT_STEERING_RECEIPT",
    "EVENT_SUBAGENT_DELEGATION",
    "EVENT_TASK_CLAIM_RECEIPT",
    "EVENT_TASK_MAILBOX_MESSAGE",
    "EVENT_TASK_RESOURCE_RELEASE",
    "EVENT_TASK_RESUMED",
    "EVENT_TASK_SUSPENDED",
    "EVENT_TEMPLATE_COMPRESSION_RECEIPT",
    "EVENT_TEMPLATE_COMPRESSION_RESTORE",
    "EVENT_THREAD_APPROVAL",
    "EVENT_TOURNAMENT_SELECTION",
    "EVENT_WEBHOOK_NODE_RECEIPT",
    "EVENT_WEBHOOK_PAYLOAD_ANCHOR",
    "EVENT_WORK_LEDGER_ANCHOR",
    "GATE_RESOLUTIONS",
    "GATE_TERMINAL_RESOLUTIONS",
    "AuditChainStore",
    "ClearanceResolutionRefusal",
    "ComputerUseActionDetails",
    "CostProfileReportDetails",
    "EvalAbComparisonDetails",
    "ForkSnapshotDetails",
    "MandateConsentReceiptDetails",
    "MemoryWriteDetails",
    "MultimodalAttachDetails",
    "PaymentReceiptDetails",
    "SkillInstallReceiptDetails",
    "SkillVerificationRefusalDetails",
    "ThreadApprovalDetails",
    "reconstruct_mcp_call_order",
    "record_a2a_message_receipt",
    "record_activity_result",
    "record_adapter_admission_receipt",
    "record_adapter_canary_receipt",
    "record_adapter_floor_update_receipt",
    "record_adapter_spawn_preflight_receipt",
    "record_adapter_version_posture_receipt",
    "record_audit_receipt_export",
    "record_automation_action",
    "record_cache_dedup_claim",
    "record_cache_eviction",
    "record_cache_hit",
    "record_cache_miss",
    "record_capability_refusal",
    "record_capability_selection",
    "record_checkpoint_retry",
    "record_claim_journal_receipt",
    "record_computer_use_action",
    "record_context_capsule",
    "record_cost_batch_route",
    "record_cost_dispatch_receipt",
    "record_cost_profile_report",
    "record_dashboard_token_grant",
    "record_delegation_minted",
    "record_endpoint_certification",
    "record_escalation_receipt",
    "record_eval_ab_comparison",
    "record_eval_gate_revocation",
    "record_eval_gate_verdict",
    "record_evidence_bundle",
    "record_expectation_expired",
    "record_fleet_conn_create",
    "record_fleet_conn_refuse",
    "record_fleet_conn_resolve",
    "record_fleet_conn_rotate",
    "record_fleet_context_activate",
    "record_fleet_var_set",
    "record_fork_snapshot",
    "record_gate_adjudication",
    "record_governance_decision",
    "record_input_refusal",
    "record_intent_capsule",
    "record_intent_drift",
    "record_mandate_consent_receipt",
    "record_mandate_revocation",
    "record_mcp_stateless_call",
    "record_mcp_task_handle",
    "record_memory_write",
    "record_mission_digest_receipt",
    "record_mission_phase_receipt",
    "record_multimodal_attach",
    "record_odata_writeback",
    "record_otel_projection",
    "record_payment_authorized",
    "record_payment_refused",
    "record_plugin_conformance_receipt",
    "record_plugin_install_receipt",
    "record_plugin_update_receipt",
    "record_pool_claim_receipt",
    "record_pool_override_refused",
    "record_pool_placement_receipt",
    "record_pool_registered",
    "record_pool_retired",
    "record_pool_updated",
    "record_pool_warm_quarantine",
    "record_pool_worker_enrolled",
    "record_process_reap_receipt",
    "record_provenance_quarantine",
    "record_provider_state_mutation",
    "record_recipe_fire",
    "record_recipe_fleet_apply",
    "record_recipe_lineage_resolve",
    "record_recipe_pause",
    "record_recipe_register",
    "record_recipe_rollback",
    "record_recipe_supersede",
    "record_render_failure",
    "record_review_board_action",
    "record_review_receipt",
    "record_routing_failover_receipt",
    "record_rule_fire_receipt",
    "record_run_artifact",
    "record_run_artifact_refused",
    "record_run_lifecycle",
    "record_run_ssh_task",
    "record_schedule_collision",
    "record_schedule_fire_projection",
    "record_sensitive_gate",
    "record_signal_gate_projection",
    "record_skill_install_receipt",
    "record_skill_usage",
    "record_skill_verification_refusal",
    "record_sla_violation",
    "record_sovereign_attestation",
    "record_sovereign_drift",
    "record_spec_requirement_set",
    "record_spiffe_svid_binding",
    "record_steering_receipt",
    "record_subagent_delegation",
    "record_taint_decision",
    "record_task_claim_receipt",
    "record_task_mailbox_message",
    "record_task_resource_release",
    "record_task_resume",
    "record_task_suspension",
    "record_thread_approval",
    "record_tournament_selection",
    "record_webhook_node_receipt",
    "record_webhook_payload_anchor",
    "record_work_ledger_anchor",
    "validate_gate_resolution",
]
