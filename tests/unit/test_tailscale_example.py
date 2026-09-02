"""Directory-driven tests for Tailscale example configurations and CLI invocations.

This test validates that:
1. All YAML config files in examples/cluster/tailscale/ are valid and loadable via load_and_validate
2. All CLI invocations found in the directory parse correctly against the real Bernstein CLI
"""

from __future__ import annotations

import shlex
from pathlib import Path

from click.core import Group

from bernstein.cli.main import cli
from bernstein.core.config.config_schema import BernsteinConfig, load_and_validate


def _discover_yaml_configs() -> list[Path]:
    """Find all YAML config files in the Tailscale examples directory."""
    config_dir = Path(__file__).resolve().parents[2] / "examples" / "cluster" / "tailscale"
    return sorted(config_dir.glob("*.yaml"))


def _join_continuations(content: str) -> str:
    """Join lines ending with a backslash-newline so multi-line shell
    commands are lexed as a single line."""
    return content.replace("\\\n", " ")


def _find_bernstein_cli_invocations() -> list[tuple[str, list[str]]]:
    """Find all CLI invocations (as text) in the Tailscale examples directory.

    Scans every file (including `#`-commented lines) for mentions of
    ``bernstein`` so that CLI examples embedded in comments are discovered.
    """
    config_dir = Path(__file__).resolve().parents[2] / "examples" / "cluster" / "tailscale"
    invocations: list[tuple[str, list[str]]] = []

    for path in sorted(config_dir.glob("*")):
        if not path.is_file():
            continue

        content = _join_continuations(path.read_text(encoding="utf-8"))
        # Split content by lines and scan for potential CLI invocations
        for _lineno, line in enumerate(content.splitlines(), start=1):
            # Strip leading comment markers and whitespace so that
            # commented-out CLI examples (``# bernstein worker ...``) are
            # still discovered.
            raw = line.strip()
            if not raw:
                continue
            if raw.startswith("#"):
                raw = raw.lstrip("#").strip()
            if not raw:
                continue

            # Look for lines that might be CLI invocations
            # This is a simple heuristic - we look for lines that contain "bernstein"
            if "bernstein" in raw.lower():
                # Parse the line as a shell command
                try:
                    lexer = shlex.shlex(raw, posix=True, punctuation_chars=True)
                    lexer.whitespace_split = True
                    tokens = list(lexer)

                    # Find the "bernstein" token
                    bernstein_idx = None
                    for i, token in enumerate(tokens):
                        if token == "bernstein":
                            bernstein_idx = i
                            break

                    if bernstein_idx is not None and bernstein_idx < len(tokens) - 1:
                        # Everything after "bernstein" is the CLI invocation
                        argv = tokens[bernstein_idx + 1 :]
                        invocations.append((str(path), argv))
                except (ValueError, IndexError):
                    # Skip lines that can't be parsed as shell commands
                    continue

    return invocations


def test_all_tailscale_configs_load_and_validate() -> None:
    """All YAML config files in examples/cluster/tailscale/ must be valid and loadable."""
    yaml_configs = _discover_yaml_configs()

    for config_path in yaml_configs:
        # Load and validate the configuration
        config = load_and_validate(config_path)

        # Basic validation - the config should be a BernsteinConfig
        assert isinstance(config, BernsteinConfig), f"Not a valid BernsteinConfig: {config_path}"


def _collect_command_params(command: Group) -> set[str]:
    """Collect all flag names (opts + secondary_opts) from a Click command."""
    params: set[str] = set()
    for p in command.params:
        params.update(p.opts)
        params.update(p.secondary_opts or ())
    return params


def _walk_cli_tree(argv: list[str]) -> tuple[bool, str]:
    """Walk argv (everything after the `bernstein` binary name) against the
    real Click command tree rooted at `bernstein.cli.main.cli`.

    Resolves the command path, then checks that every flag supplied
    after the leaf command is declared on that command (via its
    Click param ``opts`` / ``secondary_opts``). Returns (ok,
    human-readable detail).
    """
    group: Group = cli
    consumed: list[str] = []
    remaining_idx = 0

    for i, token in enumerate(argv):
        if token.startswith("-"):
            remaining_idx = i
            break
        consumed.append(token)
        command = group.commands.get(token) if isinstance(group, Group) else None
        if command is None:
            path = " ".join(consumed)
            return False, f"`bernstein {path}` is not a registered CLI command"
        if isinstance(command, Group):
            group = command
            remaining_idx = i + 1
        else:
            remaining_idx = i + 1
    else:
        remaining_idx = len(argv)

    path = " ".join(consumed)
    if not consumed:
        return True, "`bernstein` resolves (no subcommand)"

    leaf = group.commands.get(consumed[-1]) if isinstance(group, Group) else None
    if leaf is None:
        return True, f"`bernstein {path}` resolves to a real command group"

    valid_flags = _collect_command_params(leaf)
    for token in argv[remaining_idx:]:
        if not token.startswith("-"):
            continue
        if token not in valid_flags:
            return False, (f"`bernstein {path}` has no flag `{token}`; expected one of {sorted(valid_flags)}")

    return True, f"`bernstein {path}` resolves to a real command with valid flags"


def test_all_cli_invocations_parse_correctly() -> None:
    """All discovered CLI invocations in the Tailscale examples directory must parse against the real Bernstein CLI."""
    cli_invocations = _find_bernstein_cli_invocations()

    for path, argv in cli_invocations:
        ok, detail = _walk_cli_tree(argv)
        assert ok, (
            f"{path}: CLI invocation failed to parse: {detail}\n"
            f"Invoked as: {' '.join(['bernstein', *argv])}\n"
            f"Expected: Click command tree validation"
        )


def main() -> None:
    """Entry point for pytest compatibility."""
    test_all_tailscale_configs_load_and_validate()
    test_all_cli_invocations_parse_correctly()


if __name__ == "__main__":
    main()
