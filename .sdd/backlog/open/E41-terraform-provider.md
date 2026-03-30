# E41 — Terraform Provider Skeleton

**Priority:** P2
**Scope:** small (10-20 min)
**Wave:** 2 — Ecosystem & Integrations

## Problem
There is no infrastructure-as-code option for managing Bernstein Cloud resources, blocking teams that require declarative infrastructure management.

## Solution
- Create a Terraform provider skeleton at `integrations/terraform/` using the Terraform Plugin Framework (Go).
- Define three stub resources: `bernstein_workspace`, `bernstein_workflow`, `bernstein_api_key`.
- Implement CRUD operations that return mock data for now (no real API calls).
- Include `main.go`, provider schema, resource schemas, and example `.tf` files.
- Structure the project so real API integration can be added later without restructuring.

## Acceptance
- [ ] Provider compiles with `go build` without errors
- [ ] `terraform plan` with example `.tf` files runs without errors
- [ ] All three resources are defined with appropriate schema attributes
- [ ] Example Terraform configurations demonstrate resource usage
