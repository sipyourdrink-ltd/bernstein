# `.bernstein/` — Repository Governance, Scenarios, and Workflows

This directory holds Bernstein's live self-governance rules alongside ready-to-run scenario recipes and conditional workflow graphs covering software engineering, AI governance, SecOps, FinOps, data migrations, SRE operations, research, documentation, and enterprise compliance.

## Layout

| Path | Purpose |
|---|---|
| [`rules.yaml`](rules.yaml) | Declarative architecture, quality, and security rules enforced on this repository |
| [`volunteer.json`](volunteer.json) | Volunteer verification rig manifest |
| [`scenarios/`](scenarios/) | **38** multi-task scenario playbooks across `software/`, `ai-governance/`, `finops/`, `data/`, `ops/`, `enterprise/`, `research/`, `docs/`, and `growth/` (24 cross-domain playbooks also ship inside the PyPI wheel under `templates/scenarios/`) |
| [`workflows/`](workflows/) | **16** conditional multi-phase workflow graphs (`phases` + fan-out/fan-in + retry loops), alongside **15** `WorkflowSpec` templates in `templates/workflows/` and **8** team manifests in `templates/teams/` |

For full multi-department organization scaffolding across **6 presets** (`minimal`, `engineering`, `secops`, `product`, `compliance`, and `full` — up to 36 roles, 16 SOPs, 20 compiled workflows, 32 scenarios, and `role_pack_acceptance`), see [`bernstein-org-kit`](https://github.com/sipyourdrink-ltd/bernstein-addons/tree/main/plugins/org-kit) in [`bernstein-addons`](https://github.com/sipyourdrink-ltd/bernstein-addons).

## Inspect and Run

```bash
# List workspace scenarios in .bernstein/scenarios/ (or packaged wheel defaults)
bernstein schedule routine scenarios --scenarios-dir .bernstein/scenarios

# List and dry-run conditional workflow manifests in .bernstein/workflows/
bernstein workflow list
bernstein workflow run ai-governance-gate --dry-run
bernstein workflow run zero-downtime-migration --dry-run
bernstein workflow run finops-budget-remediation --dry-run

# Inspect built-in team manifests (python, typescript, fullstack, security-audit, research-ops, ai-governance, platform-sre, data-migration)
bernstein team list
bernstein team show ai-governance
bernstein team drift
```
