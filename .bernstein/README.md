# `.bernstein/` — Repository Governance, Scenarios, and Workflows

This directory holds Bernstein's live self-governance rules alongside ready-to-run scenario recipes and conditional workflow graphs covering software engineering, operations, security, research, documentation, and enterprise compliance.

## Layout

| Path | Purpose |
|---|---|
| [`rules.yaml`](rules.yaml) | Declarative architecture, quality, and security rules enforced on this repository |
| [`volunteer.json`](volunteer.json) | Volunteer verification rig manifest |
| [`scenarios/`](scenarios/) | **28** multi-task scenario playbooks across `software/`, `ops/`, `enterprise/`, `research/`, `docs/`, and `growth/` |
| [`workflows/`](workflows/) | **10** conditional multi-phase workflow graphs (`phases` + fan-out/fan-in + retry loops) |

For full organization scaffolding (6 minimal or 24 full role definitions, 7 SOPs, 11 compiled workflows, and `role_pack_acceptance`), see [`bernstein-org-kit`](https://github.com/sipyourdrink-ltd/bernstein-addons/tree/main/plugins/org-kit) in [`bernstein-addons`](https://github.com/sipyourdrink-ltd/bernstein-addons).

## Inspect and Run

```bash
# List workspace scenarios in .bernstein/scenarios/ (or packaged defaults)
bernstein routine scenarios --scenarios-dir .bernstein/scenarios

# List and dry-run conditional workflow manifests in .bernstein/workflows/
bernstein workflow list
bernstein workflow run audit-evidence-pack --dry-run
bernstein workflow run incident-response --dry-run

# Inspect built-in team manifests (python, typescript, fullstack, security-audit, research-ops)
bernstein team list
bernstein team show security-audit
bernstein team drift
```
