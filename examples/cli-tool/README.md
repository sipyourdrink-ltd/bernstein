# Example CLI Tool

A Click-based CLI tool built with Bernstein.

## Quick Start

```bash
cd examples/cli-tool
uv venv
source .venv/bin/activate
uv pip install -e .
mycli --help
```

## Commands

- `mycli greet <name>` - Greet someone
- `mycli process <file>` - Process a file
- `mycli version` - Show version

## Running with Bernstein

```bash
bernstein run --goal "Add a new command to export data as CSV"
```

## Project Structure

```
cli-tool/
├── pyproject.toml  # Package configuration
├── mycli/
│   ├── __init__.py
│   └── cli.py      # CLI commands
├── tests/          # Unit tests
└── bernstein.yaml  # Bernstein configuration
```

## Publishing to PyPI

```bash
uv build
uv publish
```
