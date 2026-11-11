# Code map — bernstein

7409 tracked files at `f5400d757fb0`.

## Modules

| Module | Files | Languages | Owner | Anchor |
|---|---|---|---|---|
| `tests` | 3427 | python, yaml | — | `tests/AGENTS.md:1` |
| `src/bernstein` | 2211 | python, yaml | — | `src/bernstein/__init__.py:1` |
| `docs` | 715 | md, js | — | `docs/.nojekyll` |
| `templates` | 200 | md, yaml | — | `templates/adapter_plugin/cookiecutter.json:1` |
| `examples` | 196 | yaml, python | — | `examples/README.md:1` |
| `web` | 108 | ts, js | — | `web/.gitignore:1` |
| `.github` | 100 | yaml, md | @chernistry | `.github/CODEOWNERS:1` |
| `scripts` | 102 | python, shell | — | `scripts/adapter_canary.py:1` |
| `<root>` | 58 | md, yaml | — | `.aider.conf.yml:1` |
| `sdk` | 36 | python, ts | — | `sdk/python/README.md:1` |
| `deploy` | 31 | yaml, md | — | `deploy/github-app/README.md:1` |
| `packages/vscode` | 30 | ts, md | — | `packages/vscode/.gitignore:1` |
| `.bernstein` | 26 | yaml | — | `.bernstein/rules.yaml:1` |
| `benchmarks` | 24 | python, yaml | — | `benchmarks/README.md:1` |
| `verify_cli` | 24 | python, md | — | `verify_cli/README.md:1` |
| `docker` | 19 | yaml, shell | — | `docker/demo/Caddyfile:1` |
| `packages/cursor-plugin` | 19 | md, shell | — | `packages/cursor-plugin/.cursor-plugin/plugin.json:1` |
| `schemas` | 15 | md | @chernistry | `schemas/agent-plugins/1.0.0/mcp.schema.json:1` |
| `packaging` | 10 | md, js | — | `packaging/deb/control:1` |
| `.cursor` | 9 | — | @chernistry | `.cursor/rules/architecture.mdc:1` |
| `.clusterfuzzlite` | 5 | python, shell | — | `.clusterfuzzlite/Dockerfile:1` |
| `integrations` | 5 | python | — | `integrations/__init__.py:1` |
| `commands` | 3 | md | — | `commands/run.md:1` |
| `.qwen` | 2 | md | @chernistry | `.qwen/.gitignore:1` |
| `eval` | 2 | python, yaml | — | `eval/metrics/pentest_scorer.py:1` |
| `proto` | 2 | — | @chernistry | `proto/bernstein/v1/cluster.proto:1` |
| `skills` | 2 | md | — | `skills/bernstein-run/SKILL.md:1` |
| `tools` | 2 | python | — | `tools/verify_audit_dsse.py:1` |
| `.devcontainer` | 1 | — | — | `.devcontainer/devcontainer.json:1` |
| `.plugin` | 1 | — | — | `.plugin/plugin.json:1` |
| `.well-known` | 1 | — | — | `.well-known/security.txt:1` |
| `Formula` | 1 | md | — | `Formula/README.md:1` |
| `action` | 1 | shell | — | `action/entrypoint.sh:1` |
| `agents` | 1 | md | — | `agents/orchestrator.md:1` |
| `community` | 17 | md | — | `community/awesome-bernstein-plugins.md:1` |
| `config` | 1 | yaml | — | `config/eu_ai_act_clause_map.yaml:1` |
| `hooks` | 1 | python | — | `hooks/smart_approve.py:1` |
| `rules` | 1 | — | — | `rules/orchestration.mdc:1` |

## Entrypoints

| Kind | Name | Target | Anchor |
|---|---|---|---|
| console-script | `bernstein` | `bernstein.cli.main:cli` | `pyproject.toml:342` |
| console-script | `bernstein-bench` | `bernstein.eval.bench.bench_cli:bench_group` | `pyproject.toml:344` |
| console-script | `bernstein-worker` | `bernstein.core.worker:main` | `pyproject.toml:343` |
| console-script | `verify-audit-receipt` | `bernstein.core.verifier.audit_receipt_verifier:main` | `pyproject.toml:345` |
| container | `Dockerfile` | `CMD curl --fail --silent --show-error http://127.0.0.1:8052/health \|\| exit 1` | `Dockerfile:50` |
| container | `Dockerfile` | `ENTRYPOINT ["bernstein"]` | `Dockerfile:60` |
| container | `Dockerfile` | `CMD ["serve"]` | `Dockerfile:61` |
| container | `docker/demo/Dockerfile` | `CMD curl --fail --silent --show-error http://127.0.0.1:8052/health \|\| exit 1` | `docker/demo/Dockerfile:51` |
| container | `docker/demo/Dockerfile` | `ENTRYPOINT ["bernstein"]` | `docker/demo/Dockerfile:56` |
| container | `docker/demo/Dockerfile` | `CMD ["conduct"]` | `docker/demo/Dockerfile:57` |
| container | `docker/volunteer-hub/Dockerfile` | `CMD curl --fail --silent --show-error http://127.0.0.1:8052/health \|\| exit 1` | `docker/volunteer-hub/Dockerfile:37` |
| container | `docker/volunteer-hub/Dockerfile` | `ENTRYPOINT ["bernstein"]` | `docker/volunteer-hub/Dockerfile:42` |
| container | `docker/volunteer-hub/Dockerfile` | `CMD ["conduct"]` | `docker/volunteer-hub/Dockerfile:43` |
| container | `docker/volunteer-rig/Dockerfile` | `ENTRYPOINT ["volunteer-rig-seed"]` | `docker/volunteer-rig/Dockerfile:19` |
| container | `examples/cluster/cloudflared/Dockerfile` | `CMD ["cloudflared", "--version"]` | `examples/cluster/cloudflared/Dockerfile:28` |
| container | `examples/cluster/cloudflared/Dockerfile` | `ENTRYPOINT ["cloudflared"]` | `examples/cluster/cloudflared/Dockerfile:30` |
| container | `examples/cluster/cloudflared/Dockerfile` | `CMD ["tunnel", "--no-autoupdate", "--metrics", "0.0.0.0:2000", "run"]` | `examples/cluster/cloudflared/Dockerfile:31` |
| module-main | `src/bernstein/__main__.py` | `src/bernstein/__main__.py` | `src/bernstein/__main__.py:1` |
| module-main | `src/bernstein/mcp/__main__.py` | `src/bernstein/mcp/__main__.py` | `src/bernstein/mcp/__main__.py:1` |
| module-main | `verify_cli/bernstein_verify/__main__.py` | `verify_cli/bernstein_verify/__main__.py` | `verify_cli/bernstein_verify/__main__.py:1` |
| module-main | `verify_cli/bernstein_verify_envelope/__main__.py` | `verify_cli/bernstein_verify_envelope/__main__.py` | `verify_cli/bernstein_verify_envelope/__main__.py:1` |
| module-main | `verify_cli/bernstein_verify_receipt/__main__.py` | `verify_cli/bernstein_verify_receipt/__main__.py` | `verify_cli/bernstein_verify_receipt/__main__.py:1` |

## Ownership

| Pattern | Owners | Anchor |
|---|---|---|
| `/.github/` | @chernistry | `.github/CODEOWNERS:16` |
| `/src/bernstein/core/` | @chernistry @vaibhav8a @thegoodengineer @tenequm @Phoenix1504e @Chirag6722 | `.github/CODEOWNERS:21` |
| `/src/bernstein/core/security/` | @chernistry | `.github/CODEOWNERS:22` |
| `/src/bernstein/core/identity/` | @chernistry | `.github/CODEOWNERS:23` |
| `/src/bernstein/core/sandbox/` | @chernistry | `.github/CODEOWNERS:24` |
| `/src/bernstein/core/tokens/` | @chernistry | `.github/CODEOWNERS:25` |
| `/src/bernstein/evolution/` | @chernistry | `.github/CODEOWNERS:26` |
| `/src/bernstein/adapters/` | @chernistry @vaibhav8a @thegoodengineer @tenequm @Phoenix1504e @Chirag6722 | `.github/CODEOWNERS:27` |
| `/schemas/` | @chernistry | `.github/CODEOWNERS:28` |
| `/proto/` | @chernistry | `.github/CODEOWNERS:29` |
| `/pyproject.toml` | @chernistry | `.github/CODEOWNERS:30` |
| `/uv.lock` | @chernistry | `.github/CODEOWNERS:31` |
| `/SECURITY.md` | @chernistry | `.github/CODEOWNERS:32` |
| `/GOVERNANCE.md` | @chernistry | `.github/CODEOWNERS:33` |
| `/MAINTAINERS.md` | @chernistry | `.github/CODEOWNERS:34` |
| `/AGENTS.md` | @chernistry | `.github/CODEOWNERS:35` |
| `/docs/governance/` | @chernistry | `.github/CODEOWNERS:36` |
| `/docs/decisions/` | @chernistry | `.github/CODEOWNERS:37` |
| `/sdk/python/pyproject.toml` | @chernistry | `.github/CODEOWNERS:41` |
| `/sdk/python/uv.lock` | @chernistry | `.github/CODEOWNERS:42` |
| `/sdk/typescript/package-lock.json` | @chernistry | `.github/CODEOWNERS:43` |
| `/web/package-lock.json` | @chernistry | `.github/CODEOWNERS:44` |
| `/packages/vscode/package-lock.json` | @chernistry | `.github/CODEOWNERS:45` |
| `/CLAUDE.md` | @chernistry | `.github/CODEOWNERS:57` |
| `/CONVENTIONS.md` | @chernistry | `.github/CODEOWNERS:58` |
| `/.aider.conf.yml` | @chernistry | `.github/CODEOWNERS:59` |
| `/.goosehints` | @chernistry | `.github/CODEOWNERS:60` |
| `/.mcp.json` | @chernistry | `.github/CODEOWNERS:61` |
| `/mcp.json` | @chernistry | `.github/CODEOWNERS:62` |
| `/.cursor/` | @chernistry | `.github/CODEOWNERS:63` |
| `/.qwen/` | @chernistry | `.github/CODEOWNERS:64` |
| `/Dockerfile` | @chernistry | `.github/CODEOWNERS:72` |
| `/docker-compose.yaml` | @chernistry | `.github/CODEOWNERS:73` |
| `/docker/sandbox/` | @chernistry | `.github/CODEOWNERS:74` |
| `/scripts/researcher_sandbox.sh` | @chernistry | `.github/CODEOWNERS:75` |
| `/scripts/quorum_check.py` | @chernistry | `.github/CODEOWNERS:80` |
| `/scripts/queue_hygiene.py` | @chernistry | `.github/CODEOWNERS:81` |
| `/renovate.json` | @chernistry | `.github/CODEOWNERS:82` |
