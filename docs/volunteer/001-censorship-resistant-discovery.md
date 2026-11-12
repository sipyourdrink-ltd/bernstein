# ADR-001: Censorship-Resistant Discovery and Transport for Volunteer Protocol

**Status**: Proposed
**Date**: 2026-08-28
**Trigger**: A real cohort of donors in a blocked jurisdiction, running local/self-hosted models (no foreign provider API), for whom the coordination layer is the only remaining chokepoint.
**Dependencies**: #3883 — transport-neutral protocol layer

---

## The Trigger (When This Work Fires)

This work is **deferred** until the trigger fires. The trigger is the existence of:

> A real cohort of donors in a blocked jurisdiction, running local/self-hosted models (so no foreign provider API is needed), for whom the coordination layer is the only remaining chokepoint.

Until that cohort exists, the work stays deferred. This decision doc is written now so the path is ready when needed.

**How to verify the trigger has fired:**
- [ ] Document (from this issue or the GitHub thread) showing donors using local models in a blocked jurisdiction
- [ ] Coordination requests being blocked while model inference works
- [ ] Explicit confirmation from the cohort that a censorship-resistant coordination layer is the missing piece

---

## Why the Trigger Concentrates Value

For a donor who uses a foreign provider API (e.g., OpenAI, Anthropic), they already need VPN/proxy to reach that API. The VPN/proxy they use for the API also carries coordination traffic with negligible marginal cost. The marginal value of a censorship-resistant coordination layer for this donor is near zero.

**The real value concentrates in the local-model cohort:**
- No VPN/proxy available (local models run on local hardware)
- No foreign provider coordination traffic to piggyback on
- Coordination layer becomes a stand-alone target for blocking

This is why the trigger is narrowly defined.

---

## Transport Options for Signed Protocol Documents

The volunteer protocol documents (#3883) are already transport-neutral. The envelope format (DSSE/in-toto) carries no network topology assumptions—only signed canonical bytes. Below are options for delivering documents across potential blocking scenarios, scored on three operational metrics.

### Option 1: Multi-Mirror Git Indexes

**Description**: Maintain multiple git repositories that mirror each other. Volunteers push results to any accessible mirror; donors fetch from whichever mirror is reachable.

| Criterion | Score | Rationale |
|---|---|---|
| Solo-maintainer operational cost | Medium | Need to sync multiple repos; can use GitHub Actions cron or simple cron jobs |
| Sybil/abuse exposure | Medium | Mirrors can be gamed if not properly verified; but signature verification already catches forged documents |
| NAT traversal | Good | Git works over HTTPS; mirrors can be hosted anywhere with egress |

**Verdict**: Low-friction first step. Add mirrors incrementally. Doesn't fundamentally change the protocol.

### Option 2: Relay/Rendezvous Servers

**Description**: Central relay server that forwards signed documents between parties. Relays don't store or interpret—only forward.

| Criterion | Score | Rationale |
|---|---|---|
| Solo-maintainer operational cost | High | Requires always-on server; TLS certificate management |
| Sybil/abuse exposure | Medium | Relays can drop traffic (censorship vector) but can't forge; operators need reputation systems |
| NAT traversal | Good | Connections outbound from volunteers; donors initiate to relay |

**Verdict**: Higher operational burden. Redundant with mirror approach for blocking scenarios.

### Option 3: Gossip Between Hubs

**Description**: Hubs exchange documents via gossip. Each hub maintains partial view of the network; propagation is eventual.

| Criterion | Score | Rationale |
|---|---|---|
| Solo-maintainer operational cost | High | Complex distributed state sync; epidemic broadcast protocols |
| Sybil/abuse exposure | High | Gossip can amplify malicious content; need anti-entropy verification |
| NAT traversal | Good | Hub-to-hub connections can be arranged; volunteers connect to closest hub |

**Verdict**: Overkill for initial cohort size. Consider if volunteer base grows to hundreds.

### Option 4: DHT Discovery

**Description**: Distributed hash table maps document IDs to network locations. Peers join DHT and query for documents by hash.

| Criterion | Score | Rationale |
|---|---|---|
| Solo-maintainer operational cost | High | DHT bootstrap nodes; Kademlia-style protocol implementation |
| Sybil/abuse exposure | High | DHTs are Sybil-prone by design; content verification required |
| NAT traversal | Poor | Node discovery through NAT is problematic (hole punching required) |

**Verdict**: Too complex for solo maintainer; NAT traversal issues for donors behind full cone NAT.

### Option 5: Onion Transport (Opt-In)

**Description**: Volunteers and donors can connect via Tor onion services for coordination traffic. Opt-in, not default.

| Criterion | Score | Rationale |
|---|---|---|
| Solo-maintainer operational cost | Medium | Run onion service; requires Tor service; hidden service setup |
| Sybil/abuse exposure | Low | Identity remains signed; onion is transport-only |
| NAT traversal | Good | Tor provides NAT traversal; no manual port forwarding |

**Verdict**: Strong supplementary option for donors in high-censorship regimes. Opt-in preserves non-onion users.

---

## Scoring Summary

| Option | Solo-Maintainer Cost | Sybil Exposure | NAT Traversal | Notes |
|---|---|---|---|---|
| Multi-Mirror Git | Medium | Medium | Good | **Recommended first step** |
| Relay/Rendezvous | High | Medium | Good | Secondary option for throughput |
| Gossip Between Hubs | High | High | Good | Future scale option |
| DHT Discovery | High | High | Poor | Not recommended |
| Onion Transport | Medium | Low | Good | Supplementary, opt-in |

---

## Confirmation: Transport-Neutral Protocol Layer

**Checklist**: The transport-neutral protocol layer (#3883) can carry a new transport without changing any message format.

- [x] Documents are serialized via `canonical_bytes()` in `documents.py`
- [x] `Envelope.payload_b64` carries opaque base64-encoded payload
- [x] `VOLUNTEER_DOCUMENT_PREDICATE_TYPE` is a URL, not a network address
- [x] Document kind discriminator is internal to predicate, not transport-dependent
- [x] `ConformanceHarness` proves documents survive hub projection (plain JSON) and GitHub projection (fenced JSON) without loss
- [x] No transport-specific fields in the schema
- [x] Transport discovery mechanism (mirror selection, relay discovery, onion address) can be layered *outside* the signed document

**Verdict**: Confirmed. Adding transport options requires no changes to document format.

---

## Honest Limit

The fundamental constraint:

> For a donor who uses a foreign provider API, the VPN/proxy they already need for that API also carries coordination traffic, so the marginal value of a censorship-resistant coordination layer concentrates in the local-model cohort.

This is the honest limit. Any solution must work within it.

---

## Recommended Phased Answer

**Phase 1 (When trigger fires)**: Multi-mirror git indexes
1. Document initial mirror set (at least 2 geographic regions)
2. Update `bernstein volunteer publish` to push to multiple remotes
3. Update `bernstein volunteer fetch` to try mirrors in order
4. Add simple cron-based mirror sync as fallback

**Phase 2 (Optional, if onion access needed)**: Onion transport
1. Document how to run hub as onion service
2. Update manifest to include optional onion address
3. Add Tor-aware fetching option in volunteer fetch

**Phase 3 (Scale, if needed)**: Gossip between hubs
1. Design distributed sync protocol
2. Add hub-to-hub exchange capability

Avoid DHT and relay for now—they're higher complexity than justified by the initial cohort size.

---

## Security Considerations

- **Signed identity preserved**: All anonymization is transport-only. Documents remain signed to accountable keys.
- **Opt-in onion**: Never a way to submit anonymously. Onion is a transport, not an identity layer.
- **Mirror verification**: Use existing signature verification; mirrors cannot forge documents.
- **No trust in content delivery**: Receiver verifies `Envelope.sig` independently of how it was delivered.

---

## References

- Issue #3888: Original spike request
- Issue #3883: Transport-neutral protocol documents
- ADR-001 (this doc): Decision record
- `docs/volunteer/volume.md`: Volunteer program overview (if exists)