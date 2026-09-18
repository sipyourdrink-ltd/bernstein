# Code map — bernstein

7303 tracked files at `1567ca0397b6`.

## Modules

| Module | Files | Languages | Owner | Anchor |
|---|---|---|---|---|
| `tests` | 3335 | python, yaml | @vaibhav8a @thegoodengineer @tenequm @Phoenix1504e | `tests/AGENTS.md:1` |
| `src/bernstein` | 2190 | python, yaml | @vaibhav8a @thegoodengineer @tenequm @Phoenix1504e | `src/bernstein/__init__.py:1` |
| `docs` | 751 | md, js | @vaibhav8a @thegoodengineer @tenequm @Phoenix1504e | `docs/.nojekyll` |
| `templates` | 200 | md, yaml | @vaibhav8a @thegoodengineer @tenequm @Phoenix1504e | `templates/adapter_plugin/cookiecutter.json:1` |
| `examples` | 196 | yaml, python | @vaibhav8a @thegoodengineer @tenequm @Phoenix1504e | `examples/README.md:1` |
| `web` | 107 | ts, js | @vaibhav8a @thegoodengineer @tenequm @Phoenix1504e | `web/.gitignore:1` |
| `.github` | 97 | yaml, md | @chernistry | `.github/CODEOWNERS:1` |
| `scripts` | 95 | python, shell | @vaibhav8a @thegoodengineer @tenequm @Phoenix1504e | `scripts/adapter_canary.py:1` |
| `<root>` | 55 | md, yaml | @vaibhav8a @thegoodengineer @tenequm @Phoenix1504e | `.aider.conf.yml:1` |
| `sdk` | 36 | python, ts | @vaibhav8a @thegoodengineer @tenequm @Phoenix1504e | `sdk/python/README.md:1` |
| `deploy` | 31 | yaml, md | @vaibhav8a @thegoodengineer @tenequm @Phoenix1504e | `deploy/github-app/README.md:1` |
| `packages/vscode` | 30 | ts, md | @vaibhav8a @thegoodengineer @tenequm @Phoenix1504e | `packages/vscode/.gitignore:1` |
| `.bernstein` | 26 | yaml | @vaibhav8a @thegoodengineer @tenequm @Phoenix1504e | `.bernstein/rules.yaml:1` |
| `benchmarks` | 24 | python, yaml | @vaibhav8a @thegoodengineer @tenequm @Phoenix1504e | `benchmarks/README.md:1` |
| `verify_cli` | 24 | python, md | @vaibhav8a @thegoodengineer @tenequm @Phoenix1504e | `verify_cli/README.md:1` |
| `docker` | 19 | yaml, shell | @vaibhav8a @thegoodengineer @tenequm @Phoenix1504e | `docker/demo/Caddyfile:1` |
| `packages/cursor-plugin` | 19 | md, shell | @vaibhav8a @thegoodengineer @tenequm @Phoenix1504e | `packages/cursor-plugin/.cursor-plugin/plugin.json:1` |
| `schemas` | 15 | md | @chernistry | `schemas/agent-plugins/1.0.0/mcp.schema.json:1` |
| `packaging` | 11 | md, js | @vaibhav8a @thegoodengineer @tenequm @Phoenix1504e | `packaging/deb/control:1` |
| `.cursor` | 9 | — | @vaibhav8a @thegoodengineer @tenequm @Phoenix1504e | `.cursor/rules/architecture.mdc:1` |
| `.clusterfuzzlite` | 5 | python, shell | @vaibhav8a @thegoodengineer @tenequm @Phoenix1504e | `.clusterfuzzlite/Dockerfile:1` |
| `integrations` | 5 | python | @vaibhav8a @thegoodengineer @tenequm @Phoenix1504e | `integrations/__init__.py:1` |
| `commands` | 3 | md | @vaibhav8a @thegoodengineer @tenequm @Phoenix1504e | `commands/run.md:1` |
| `.qwen` | 2 | md | @vaibhav8a @thegoodengineer @tenequm @Phoenix1504e | `.qwen/.gitignore:1` |
| `eval` | 2 | python, yaml | @vaibhav8a @thegoodengineer @tenequm @Phoenix1504e | `eval/metrics/pentest_scorer.py:1` |
| `proto` | 2 | — | @chernistry | `proto/bernstein/v1/cluster.proto:1` |
| `skills` | 2 | md | @vaibhav8a @thegoodengineer @tenequm @Phoenix1504e | `skills/bernstein-run/SKILL.md:1` |
| `tools` | 2 | python | @vaibhav8a @thegoodengineer @tenequm @Phoenix1504e | `tools/verify_audit_dsse.py:1` |
| `.devcontainer` | 1 | — | @vaibhav8a @thegoodengineer @tenequm @Phoenix1504e | `.devcontainer/devcontainer.json:1` |
| `.plugin` | 1 | — | @vaibhav8a @thegoodengineer @tenequm @Phoenix1504e | `.plugin/plugin.json:1` |
| `.well-known` | 1 | — | @vaibhav8a @thegoodengineer @tenequm @Phoenix1504e | `.well-known/security.txt:1` |
| `Formula` | 1 | md | @vaibhav8a @thegoodengineer @tenequm @Phoenix1504e | `Formula/README.md:1` |
| `action` | 1 | shell | @vaibhav8a @thegoodengineer @tenequm @Phoenix1504e | `action/entrypoint.sh:1` |
| `agents` | 1 | md | @vaibhav8a @thegoodengineer @tenequm @Phoenix1504e | `agents/orchestrator.md:1` |
| `community` | 1 | md | @vaibhav8a @thegoodengineer @tenequm @Phoenix1504e | `community/awesome-bernstein-plugins.md:1` |
| `config` | 1 | yaml | @vaibhav8a @thegoodengineer @tenequm @Phoenix1504e | `config/eu_ai_act_clause_map.yaml:1` |
| `hooks` | 1 | python | @vaibhav8a @thegoodengineer @tenequm @Phoenix1504e | `hooks/smart_approve.py:1` |
| `rules` | 1 | — | @vaibhav8a @thegoodengineer @tenequm @Phoenix1504e | `rules/orchestration.mdc:1` |

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
| container | `docker/volunteer-rig/Dockerfile` | `ENTRYPOINT ["volunteer-rig-seed"]` | `docker/volunteer-rig/Dockerfile:18` |
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
| `*` | @vaibhav8a @thegoodengineer @tenequm @Phoenix1504e | `.github/CODEOWNERS:9` |
| `/.github/` | @chernistry | `.github/CODEOWNERS:13` |
| `/src/bernstein/core/` | @chernistry | `.github/CODEOWNERS:14` |
| `/src/bernstein/evolution/` | @chernistry | `.github/CODEOWNERS:15` |
| `/src/bernstein/adapters/` | @chernistry | `.github/CODEOWNERS:16` |
| `/schemas/` | @chernistry | `.github/CODEOWNERS:17` |
| `/proto/` | @chernistry | `.github/CODEOWNERS:18` |
| `/pyproject.toml` | @chernistry | `.github/CODEOWNERS:19` |
| `/uv.lock` | @chernistry | `.github/CODEOWNERS:20` |
| `/SECURITY.md` | @chernistry | `.github/CODEOWNERS:21` |
| `/GOVERNANCE.md` | @chernistry | `.github/CODEOWNERS:22` |
| `/MAINTAINERS.md` | @chernistry | `.github/CODEOWNERS:23` |
| `/AGENTS.md` | @chernistry | `.github/CODEOWNERS:24` |
| `/docs/governance/` | @chernistry | `.github/CODEOWNERS:25` |
| `/docs/decisions/` | @chernistry | `.github/CODEOWNERS:26` |
| `/scripts/quorum_check.py` | @chernistry | `.github/CODEOWNERS:31` |
| `/scripts/queue_hygiene.py` | @chernistry | `.github/CODEOWNERS:32` |
| `/renovate.json` | @chernistry | `.github/CODEOWNERS:33` |
