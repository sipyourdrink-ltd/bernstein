# D15 — Example Gallery with 20 Scenario Templates

**Priority:** P1
**Scope:** small (10-20 min)
**Wave:** 1 — Developer Love

## Problem
New users don't know how to write a `bernstein.yaml` for their specific project type. Without concrete examples, they either guess at configuration or give up.

## Solution
- Create an `examples/` directory at the repo root.
- Add 20 `bernstein.yaml` files, each in its own subdirectory with a `README.md` explaining the scenario:
  1. `fastapi-crud` — CRUD API with SQLAlchemy models
  2. `django-migration` — Django model migration and data backfill
  3. `react-component` — React component with tests and Storybook
  4. `rust-lib` — Rust library crate with docs and benchmarks
  5. `go-microservice` — Go microservice with gRPC and health checks
  6. `python-cli` — Click-based CLI tool with argument parsing
  7. `monorepo-refactor` — Cross-package refactor in a monorepo
  8. `security-audit` — Dependency and code security scan
  9. `test-coverage-boost` — Increase test coverage to a target percentage
  10. `documentation-generation` — Auto-generate API docs from code
  11. `nextjs-app` — Next.js app router page with SSR data fetching
  12. `express-middleware` — Express.js middleware chain with auth
  13. `flask-blueprint` — Flask blueprint with Marshmallow schemas
  14. `typescript-sdk` — TypeScript SDK with barrel exports and types
  15. `docker-compose` — Multi-service Docker Compose setup
  16. `terraform-module` — Terraform module with variables and outputs
  17. `github-actions` — CI/CD pipeline with test, lint, and deploy
  18. `database-schema` — Schema migration with rollback support
  19. `api-versioning` — REST API versioning strategy implementation
  20. `performance-optimization` — Profiling and optimization workflow
- Each subdirectory contains `bernstein.yaml` and `README.md` with usage instructions.

## Acceptance
- [ ] `examples/` directory exists with 20 subdirectories
- [ ] Each subdirectory contains a valid `bernstein.yaml` and a `README.md`
- [ ] Each `bernstein.yaml` is syntactically valid and uses realistic goals and models
- [ ] Each `README.md` explains the scenario, prerequisites, and how to run it
- [ ] An `examples/README.md` index file lists all examples with one-line descriptions
