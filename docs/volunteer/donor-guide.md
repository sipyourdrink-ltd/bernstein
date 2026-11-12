# Volunteer Program Donor Guide

## How to Join

### Available CLI commands

Use `bernstein volunteer browse --index URL` to list opt-in projects, `verify` to
validate a project manifest, and `budget` to set donor limits. The CLI also
provides `hub` and `verify-bundle`.

The `autopilot` command loads a donor profile. Running the loop requires
a Python `TaskSource` implementation. The CLI cannot run an individual
task from an issue URL.

### Complete Join Process

1. **Install Bernstein**
   ```bash
   uv sync
   ```

2. **Verify the Project**
   ```bash
   bernstein volunteer verify /path/to/checkout
   ```
   Run this on the project you want to donate to (or current directory) to validate its volunteer manifest and get the digest.

3. **Browse Participating Projects**
   ```bash
   bernstein volunteer browse --index https://registry.example/index.json
   ```
   Replace the example URL with your volunteer index. Repeat `--index` to
   browse multiple indexes.

4. **Set Donor Limits**
   ```bash
   bernstein volunteer budget --budget-tasks 10 --budget-hours 6 --max-size m
   ```

## Federated index discovery (Python API)

Federation is an explicit opt-in for Python callers. The existing
`bernstein volunteer browse` command and unconfigured `browse_indexes()`
keep their flat-index behavior; CLI donor-configuration support is not included.

```python
from bernstein.core.volunteer.registry import FederationPolicy, browse_indexes

policy = FederationPolicy(
    pinned_keys={"https://registry.example/index.json": maintainer_public_key},
    denied_indexes=frozenset({"https://excluded.example/index.json"}),
    allow_unsigned=False,
    max_depth=3,
    max_indexes=100,
)
projects, dropped = browse_indexes(
    ["https://registry.example/index.json"],
    federation=policy,
)
```

`maintainer_public_key` is an Ed25519 public key acquired independently and
pinned by the donor. Pins and denials match exact index URLs. A pinned index
requires a valid signature even if `allow_unsigned=True`; malformed signatures,
wrong keys and signed documents without a pin are refused. Unpinned unsigned
indexes are accepted only with an explicit `allow_unsigned=True`. Denial takes
precedence and prevents fetching that index.

A v2 index retains the v1 `projects` rows and adds `"indexes": ["https://..."]`.
Project rows may include `added_by`, an optional maintainer identifier retained
on `IndexEntry`. To sign an index, use the existing
`bernstein.core.protocols.volunteer.documents.sign_document` with
`document_kind="registry-index"`; put the serialized DSSE envelope in a
`signature` field. The signed document is the entire index **without** that
field, including child URLs, project rows and unknown extensions. Verification
checks both the pinned signature and equality with the delivered index.

Traversal is breadth-first. Root depth is zero; a depth limit of zero reads
only the configured roots. The index-count budget bounds scheduled URLs and
actual fetches, including failed fetches. Exact URLs identify nodes: mirrors
with identical content are distinct and consume the budget separately. Cycles
terminate and name their members; shared children in a diamond are not cycles.

`resolve_indexes(urls, policy=policy, transport=...)` returns a frozen
`FederationResolution` with entries, fetched URLs, skipped indexes/reasons,
cycle members and conflicting project URLs. `passed` means no refusal or
budget truncation; `as_record()` provides structured diagnostics. A caller can
inject the existing `HTTPTransport` for offline operation or testing.

Indexes remain pointers. Conflicting row hints, including cached manifest
digests, are reported without deciding project policy. Entries merge through
the existing first-pointer deduplication. `browse_indexes()` then loads the
project's actual `.bernstein/volunteer.json`; its license, status, budget and
local-execution policy determine acceptance and its own digest is returned.
The resolver does not fetch project manifests or distribute keys.

## Budget Controls

### Wall-Clock Budget
- Each task has a configured maximum runtime (default: 24 hours = 1440 minutes)
- Projects can set stricter limits in their `volunteer.json`
- `bernstein volunteer browse --budget <minutes>` already filters the offered
  projects to what fits the wall clock you are willing to give; the same figure
  becomes the runner's own limit once it lands
- Exceeding the budget results in automatic task termination

### Memory Limits
- Default: 2048 MB RAM per task
- Projects can set minimum requirements
- Donors can adjust within project limits
- Tasks that exceed memory limits are killed

### Resource Enforcement
- CPU quota: 200,000 microseconds per 100ms period
- Wall clock hard limit: prevents indefinite running
- Tasks that exceed limits are terminated with documented reasons

## What Runs Where

### Isolation Layers

**Donor Machine**
- The volunteer worker runs as a regular process on the donor's machine
- No special permissions or capabilities granted beyond what the donor provides
- The donor controls all system-level access

**Worktree Isolation**
- Each task runs in an isolated git worktree
- Changes are confined to that worktree, not the main repository
- Worktree persists across task runs for the same donor

**Sandbox Backend**
- **MicroVM**: Hardware-enforced isolation (preferred)
- **Container-userns**: Kernel-level user namespace isolation
- **Container**: Shared kernel (only with explicit donor consent)
- The sandbox boundary is derived from the project manifest + donor limits

### Network Access

**Denied by Default**
- No internet access unless explicitly allowed
- Projects declare specific hosts in `egress_allowlist`
- Package registries (Python PyPI, npm, crates.io, etc.) are always included

**Controlled by Manifest**
- Empty allowlist = network off
- Specific hosts added by project
- Donor cannot loosen beyond project policy (read-only constraint)

### Environment Variables

**Allowed List**
- Only specific environment variables reach the sandbox
- `PATH`, `HOME`, locale settings, and Bernstein markers
- No credentials, tokens, or host environment variables
- Environment is built purely from the sandbox profile

## How to Stop

### Graceful Exit

**Task Completion**
```bash
# Task completes automatically when gates pass
# Receipt is generated and signed
```

**Task Release**

The CLI has no manual task-release command. A hub lease that stops receiving
heartbeats expires after its configured timeout.

### Cleanup

**Worktree Persistence**
- Worktrees are retained after task completion
- Multiple tasks for same donor use same worktree
- Prevents redundant checkout operations

**Manual Cleanup**
```bash
# Remove worktree if needed:
rm -rf .git/worktrees/donor-<identifier>
```

## Security & Trust

### What You Accept

**Running Untrusted Code**
- You run code from strangers on your hardware
- The code comes from the issue repository + model's interpretation
- You control what gets checked out and executed

**Containment Limits**
- The sandbox profile is derived from the project manifest + your limits
- You cannot loosen containment beyond what the project allows
- The receipt binds to the exact containment decision

**Verification Delay**
- Verification happens months later (offline capability)
- Maintainers rebuild profiles from committed manifests
- No real-time feedback during execution

### What You Control

**Resource Budgets**
- You set wall-clock and memory limits
- You choose which issues to run
- You control which GitHub identity claims tasks

**Local Environment**
- Your machine hardware and OS are the isolation boundary
- You control software dependencies and system configuration
- You decide which projects to donate to

## Documentation & References

### Key Documents
- [Volunteer Threat Model](threat-model.md) - Security boundaries and risks
- [Volunteer Manifest Reference](reference/volunteer-manifest.md) - Project opt-in schema
- [Volunteer Runner Reference](reference/volunteer-runner.md) - End-to-end execution flow

### Commands & Help
```bash
# Full volunteer program help
bernstein volunteer --help

# Specific subcommand help
bernstein volunteer verify --help
bernstein volunteer browse --help
bernstein volunteer hub --help
```

## Troubleshooting

### Common Issues

**Task Refused**
- Check that the issue is labeled with the correct volunteer label
- Verify the project's `volunteer.json` exists and is valid
- Ensure your volunteer worker is up to date

**Network Access**
- Projects with empty egress_allowlist have no network
- Package registries are always included
- Check project manifest for specific allowed hosts

**Resource Limits**
- Monitor task resource usage
- Adjust budgets if tasks consistently hit limits
- Review project requirements vs. your machine capabilities

### Getting Help
- Review the volunteer threat model for security boundaries
- Check the project documentation for specific gate requirements
- Contact the project maintainer for guidance

## Program Philosophy

The volunteer program operates on a simple principle: **donate compute, maintain trust**

- **Simplicity over complexity**: No accounts, no sign-ups, no central coordination
- **Verification over trust**: Receipts are cryptographically verifiable months later
- **Containment over permissions**: Sandbox boundary is content-addressed and immutable
- **Flexibility over rigidity**: Projects declare what they need, donors decide what they provide

This guide documents the donor's perspective. For project maintainers, see the [Volunteer Program README](README.md).
