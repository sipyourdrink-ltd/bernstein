# F94 — Edge Computing Mode

**Priority:** P5
**Scope:** medium (20 min for skeleton/foundation)
**Wave:** 5 — Future-Proofing 2030-2035

## Problem
Users with local GPU hardware, on-prem requirements, or poor internet connectivity cannot run bernstein agents on their own devices without cloud dependency.

## Solution
- Build an edge computing mode that orchestrates agents running on local devices (laptop, Raspberry Pi, on-prem GPU servers)
- Use mDNS (zeroconf) for automatic peer discovery on the local network
- Each device runs a `bernstein edge agent` daemon advertising its capabilities and resources
- Orchestrator discovers available edge agents and distributes tasks based on device capabilities (GPU, memory, CPU)
- All communication stays on local network — zero cloud dependency
- Implement P2P task distribution: orchestrator sends task to best-fit edge agent, receives result directly
- Add `bernstein edge status` showing all discovered devices and their load

## Acceptance
- [ ] `bernstein edge agent` daemon runs on local devices and advertises via mDNS
- [ ] Orchestrator discovers edge agents on local network automatically
- [ ] Tasks distributed to agents based on device capabilities (GPU, memory, CPU)
- [ ] All communication local — no cloud dependency required
- [ ] `bernstein edge status` lists discovered devices with resource utilization
- [ ] Works across Linux, macOS, and Raspberry Pi OS
