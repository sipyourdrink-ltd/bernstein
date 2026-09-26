# ADR-014: Agents Are Principals, Projected From the Chain

**Status**: Accepted
**Date**: 2026-09-20
**Context**: `src/bernstein/core/identity/` (`agent_registry.py`,
`principal.py`, `grants.py`), `src/bernstein/core/security/directory_bridge.py`,
`src/bernstein/core/security/grant_precondition.py`, issues #4969, #4970, #5022

---

## Problem

The system already treats agents as principals everywhere it matters and
nowhere it is visible. It derives a deterministic SPIFFE identity per install
and agent, issues Ed25519-signed chain-anchored grants, records delegations,
signs agent cards and scopes capability tokens. What is missing is the noun:
no object an operator can list, describe, or hand to an auditor that answers
"which agents exist here, what may each one do, and who decided that".

Two further gaps sit behind that noun. A grant is decided once at issue and
then carried, even though every fact it rested on can change before it is
spent. And operators who already run an external directory that answers "who
may do what" for humans have no reconciliation between that directory and the
agent identity plane.

## Decision

1. **The registry is a projection, not a second source of truth.** `bernstein
   identity agents` folds the verified grant and delegation chains into one
   list of `AgentPrincipal` entries. Deleting the projection and recomputing
   it from the same chain is byte-identical, and an entry the chain does not
   establish is refused rather than invented. A registry that is its own
   authority drifts; a fold over the audit chain cannot.

2. **Grants re-decide at dispatch.** The pre-dispatch interlock re-evaluates a
   grant's preconditions against current chain state before a tool call goes
   out. The outcome is still-valid, revoked, or narrowed, each an event, and
   a refusal names the chain position of the record that superseded the grant.
   Re-issue narrows, it never widens; an empty ceiling authorises nothing. The
   re-decision is a chain read, not a model call and not a network round trip.

3. **External directories sit behind one thin bridge contract.** A directory
   adapter resolves a principal, lists group memberships and reports a
   revocation. Core never imports a vendor SDK; adapters may. Every resolution
   is appended to the chain, so "the directory said this agent was in that
   group at that time" becomes a verifiable fact rather than a live lookup
   nobody can reproduce.

## Related work

NIST's Center for AI Standards and Innovation (CAISI) launched the
[AI Agent Standards Initiative](https://www.nist.gov/news-events/news/2026/02/announcing-ai-agent-standards-initiative-interoperable-and-secure)
in February 2026. One of the initiative's pillars is research into AI agent
security and identity, including agent authentication and identity
infrastructure.

This ADR addresses related properties through a deterministic per-install
identity, short-lived delegated grants, and a verifiable audit trail. This is
a relationship to the initiative's stated scope, not a conformance claim; no
initiative profile establishing such a conformance target has been published
yet. Revisit this section when a relevant profile or specification appears.

## Rejected alternative: make the registry its own authority

A registry that is written by admission and only occasionally reconciled
drifts from what actually happened. Rejected because the audit chain already
holds every fact the registry needs; a second authority is a second thing to
keep consistent, and the divergence is exactly what an auditor asks about.

## Rejected alternative: keep the directory separate from the chain

The path of least resistance is to resolve groups against a live directory and
discard the answer. Rejected because a live lookup is unreproducible after the
fact: nobody can say later what the directory answered when the decision was
made. Recording the resolution is what turns it from an opinion into evidence.

## Rejected alternative: one vendor SDK per directory

Rejected because it puts a third party's release cadence inside the trust
boundary and makes each new directory a core change. The bridge is a protocol,
so an adapter satisfies it structurally without importing Bernstein at all.

## Consequences

### Benefits

The identity plane becomes listable, attributable and recomputable, and a
grant that is valid at issue and invalid at use is refused with a reason a
third party can verify offline.

### Costs

The dispatch-time re-decision runs on every call, so it must stay cheap. It
is kept off the hot path by an index that reads only bytes appended since the
last call; a design that walks the chain per dispatch would have to be
replaced. A directory answer is as good as the directory, and the record says
so by naming the adapter and the age of the answer.
