# Volunteer Threat Model

## Security Surface

### Hostile Input Posture

**Issue Text**: Untrusted repository content served as agent prompts
- Source: GitHub issue title + body, from repositories the donor does not control
- Trust level: Zero - the donor never sees, validates, or sanitizes the input
- Channel: Becomes the `prompt` argument at `CLIAdapter.spawn()` without prior normalization

**Gates Commands**: Argument vectors from untrusted repositories
- Shape: `["uv", "run", "pytest", "-q"]` (not shell strings)
- Trust level: Zero - gates run untrusted code, the foundation of volunteer work
- Channel: Direct execution without shells; the entire program exists to close this gap

### Sandbox Boundary

The volunteer program isolates untrusted execution behind three layers:

**Backend Isolation**
- MicroVM (preferred) - user-space kernel, hardware-enforced boundary
- Container-userns - kernel isolation with user namespace
- Container - shared kernel (dual opt-in with manifest and donor consent)

**Egress Control**
- Deny-all by default
- Project-declared allowlist plus package registries
- Empty allowlist = network off, not "off by convention"

**Environment Filter**
- Allowlist only (no denylist)
- `PATH`, `HOME`, locale, and specific markers only
- Never inherits host environment (`os.environ` is never read)

## Accepted Risks

### PoC Limitations (Explicitly Documented)

**Claim Races**
- Two donors can independently pick the same issue (no coordinator lock)
- This is not a bug; it's an accepted race documented as such
- Second donor sees first's claim comment and steps aside (after staleness window)
- Prevents total duplication but allows occasional race windows

**Donor-Side-Only Verification**
- Receipt verification is maintainer-side only
- The donor validates against their own checkout, never against a central authority
- A receipt is a signed bundle that proves the policy was followed and the work was contained
- No central attestation of work completion

**Exfiltration via Gates**
- If a donor runs a malicious gate, they can exfiltrate data through that gate
- The sandbox boundary is between the gate runner and the agent, not between the agent and external networks
- Mitigation: donor chooses which gates to run; containment is opt-in per project

**Social Engineering via Issue Text**
- Attackers can craft issues to influence the agent's prompt
- The only protection is the program's three-channel sanitization
- HTML comments, invisible characters, and lookalikes are stripped from all repository text before reaching the model

### What the Program Does NOT Protect Against

1. **Exfiltrated Credentials**: If the donor's machine has compromised gates or other software, those can exfiltrate data
2. **Network Bypass**: The sandbox profile's egress allowlist is comprehensive but not perfect; determined actors might find gaps
3. **Supply Chain**: The donor's environment may have compromised dependencies
4. **Social Engineering**: Beyond input sanitization, there's no psychological protection against prompt injection
5. **Hardware Vulnerabilities**: The sandbox backend may have exploitable vulnerabilities

## Security Guarantees

### What IS Guaranteed

1. **No Shells**: Never runs a shell on untrusted text. Gates are argv vectors, not shell strings.
2. **Containment Boundary**: The sandbox profile digest is content-addressable and verifiable months later.
3. **Input Sanitization**: Three independent channels close the render-versus-decode gap.
4. **Deterministic Derivation**: Profile = manifest.digest + donor limits → content-addressable decision.
5. **No Environment Inheritance**: Adapter credentials are isolated from the sandbox environment.

### What the Receipt Binds To

A receipt bundle attests:
1. **Which Gates Ran**: The argv vectors the submission executed
2. **What They Produced**: The resulting PR or file changes
3. **The Sandbox Profile**: Manifest digest + donor limits (content-addressable)
4. **Signature**: Cryptographic proof the worker controlled the signing key

A maintainer rebuilds the profile from the manifest at the submitted commit and compares digests. Mismatch = refused.

## Threat Model Summary

### Primary Threats
1. **Input Poisoning**: Malicious repository content becoming agent prompts
2. **Evasion**: Bypassing containment through kernel exploits or network holes
3. **Race Conditions**: Two donors claiming the same issue simultaneously
4. **Social Engineering**: Psychological manipulation of the agent's behavior

### Secondary Threats
1. **Supply Chain Compromise**: Malicious dependencies in the project's ecosystem
2. **Hardware Vulnerabilities**: Exploits in the sandbox backend
3. **Configuration Errors**: Misconfigured allowlists or limits

### Accepted Trade-offs
1. **Coordinator Overhead**: No locking mechanism for donor coordination (simplicity vs. perfect deduplication)
2. **Donor Trust**: Donor chooses which gates to run (flexibility vs. centralized safety)
3. **Verification Delay**: Verification happens months later (offline capability vs. immediate feedback)

## Security Testing

### Canary Tests
- `:func:VolunteerSandboxProfile.digest` matches rebuild
- `:func:profile_matches` verifies against expected digest
- `:func:describe_refusal` captures refusals for audit trails

### Integrity Guarantees
- Profile fields are frozen in the digest (no runtime knobs)
- Manifest fields cannot change normalization without breaking receipts
- Unknown fields are preserved (not dropped) to maintain compatibility

## Honest Limitations

1. **No Perfect Isolation**: The program assumes the donor's hardware and chosen backend are trustworthy
2. **No Behavioral Guarantees**: Sanitization closes text channels, not psychological ones
3. **No Central Coordination**: Multiple donors can race (accepted limitation)
4. **No Perfect Network Control**: Egress allowlist is comprehensive but not foolproof

## Security Recommendations

### For Projects
1. **Declare Strict Policy**: Use microVM sandbox and restrictive allowlists
2. **Audit Gates**: Review all gate commands for unintended data flows
3. **Limit Duration**: Set reasonable wall clock limits
4. **Monitor Results**: Review receipts for anomalies

### For Donors
1. **Understand Risks**: Know that you run untrusted code on your machine
2. **Maintain Environment**: Keep your system and dependencies updated
3. **Follow Guidelines**: Use the program as documented, not as you guess
4. **Report Issues**: Report any suspected violations or bypasses

### For Maintainers
1. **Verify Receipts**: Always rebuild and compare digests
2. **Update Profiles**: Keep the sandbox profile documentation current
3. **Audit Code**: Review security claims in the codebase
4. **Educate Users**: Document risks and limitations clearly

## References

- `src/bernstein/core/volunteer/sandbox_profile.py` - Containment boundary derivation
- `src/bernstein/core/volunteer/manifest.py` - Project policy declaration
- `src/bernstein/core/volunteer/issue_sanitize.py` - Input sanitization channels
- `src/bernstein/core/volunteer/claim.py` - Coordinator-free claim etiquette
- `docs/sandbox/*` - Individual sandbox backend limitations
