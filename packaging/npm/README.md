# bernstein-orchestrator

Declarative agent orchestration for engineering teams.

Orchestrate multiple AI coding agents (Claude Code, Codex, Gemini CLI, Cursor)
in parallel. One YAML config, deterministic scheduling, verified output.

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
