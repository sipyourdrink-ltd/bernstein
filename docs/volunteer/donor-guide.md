# Volunteer Program Donor Guide

## How to Join

### One-Liner CLI Command

> **Not available yet.** `bernstein volunteer run` is the intended entry point
> and lands with the one-command onboarding work (#3889). Today the shipped
> subcommands are `bernstein volunteer verify`, `browse` and `hub`; the rest of
> this guide describes the shape the runner is being built to, so a donor can
> tell in advance what it will and will not be allowed to do on their machine.

```bash
bernstein volunteer run https://github.com/owner/project/issues/123
```

This is the command a donor will run to start a volunteer task. It takes a
GitHub issue URL and begins the execution process.

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

3. **Browse Available Issues**
   ```bash
   bernstein volunteer browse
   ```
   List all issues labeled for volunteers across all indexes (donor needs to configure indexes first).

4. **Run a Task**
   ```bash
   bernstein volunteer run <issue-url>
   ```
   Replace `<issue-url>` with the exact GitHub issue URL you want to work on.

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

Interrupting the worker process abandons the task and the lease it holds
expires on its own. `bernstein volunteer release <task-id> "<reason>"`, which
records the reason against the lease instead of waiting for the timeout, lands
with the runner (#3889) and is not available yet.

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
