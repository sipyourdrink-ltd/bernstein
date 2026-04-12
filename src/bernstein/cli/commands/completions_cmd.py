"""Shell completion generation for bash, zsh, and fish.

Generates shell-specific completion scripts using Click's built-in
completion support.  Users source the output in their shell profile.

CLI-004: Shell completions for bash, zsh, fish.
"""

from __future__ import annotations

import click


@click.command("completions")
@click.option(
    "--shell",
    type=click.Choice(["bash", "zsh", "fish"]),
    default="bash",
    show_default=True,
    help="Shell type to generate completions for.",
)
@click.pass_context
def completions_cmd(ctx: click.Context, shell: str) -> None:
    """Generate shell completion scripts.

    \b
    For bash -- add to ~/.bashrc:
      eval "$(bernstein completions --shell bash)"

    \b
    For zsh -- add to ~/.zshrc:
      eval "$(bernstein completions --shell zsh)"

    \b
    For fish -- add to ~/.config/fish/completions/bernstein.fish:
      bernstein completions --shell fish | source
    """
    from click.shell_completion import BashComplete, FishComplete, ZshComplete

    _complete_var = "_BERNSTEIN_COMPLETE"
    _prog_name = "bernstein"

    shell_cls = {"bash": BashComplete, "zsh": ZshComplete, "fish": FishComplete}[shell]
    # Walk up to the root CLI group so completions cover all subcommands.
    root_ctx = ctx
    while root_ctx.parent is not None:
        root_ctx = root_ctx.parent

    completer = shell_cls(root_ctx.command, {}, _prog_name, _complete_var)
    click.echo(completer.source())
