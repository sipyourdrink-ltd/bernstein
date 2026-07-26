# bernstein-orchestrator

Deterministic orchestrator for CLI coding agents.

Orchestrate multiple CLI coding agents (Claude Code, Codex, Gemini CLI, Cursor)
in parallel. One YAML config, no model in the coordination loop, so parallel
runs in per-task git worktrees replay byte-identically. Signed lineage plus an
opt-in HMAC audit chain.

## Install

```bash
npm install -g bernstein-orchestrator
```

Requires Python 3.12+. The wrapper delegates to the
[bernstein PyPI package](https://pypi.org/project/bernstein/).

## Usage

```bash
bernstein run plans/my-project.yaml
bernstein status
bernstein agents
```

## Links

- [GitHub](https://github.com/sipyourdrink-ltd/bernstein)
- [PyPI](https://pypi.org/project/bernstein/)
- [Documentation](https://github.com/sipyourdrink-ltd/bernstein#readme)

## License

Apache-2.0
