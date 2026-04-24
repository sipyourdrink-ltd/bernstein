"""Tests for systemd/launchd template rendering (op-004)."""

from __future__ import annotations

from bernstein.core.daemon.launchd import render_launchd_plist
from bernstein.core.daemon.systemd import (
    render_systemd_system_unit,
    render_systemd_user_unit,
)


def test_systemd_user_template_renders_known_fields() -> None:
    out = render_systemd_user_unit(
        command="bernstein dashboard --headless",
        env={"FOO": "bar", "BAZ": "qux"},
        workdir="/home/alice",
        path_env="/usr/bin:/bin",
    )
    assert "ExecStart=bernstein dashboard --headless" in out
    assert "WorkingDirectory=/home/alice" in out
    assert "Environment=PATH=/usr/bin:/bin" in out
    assert 'Environment="FOO=bar"' in out
    assert 'Environment="BAZ=qux"' in out
    assert "WantedBy=default.target" in out


def test_systemd_system_template_renders_known_fields() -> None:
    out = render_systemd_system_unit(
        command="/usr/local/bin/bernstein dashboard --headless",
        env={"BERNSTEIN_TELEGRAM_BOT_TOKEN": "secret-token"},
        workdir="/srv/bernstein",
        path_env="/usr/local/bin:/usr/bin",
    )
    assert "ExecStart=/usr/local/bin/bernstein dashboard --headless" in out
    assert "WorkingDirectory=/srv/bernstein" in out
    assert 'Environment="BERNSTEIN_TELEGRAM_BOT_TOKEN=secret-token"' in out
    assert "WantedBy=multi-user.target" in out


def test_launchd_template_renders_known_fields() -> None:
    out = render_launchd_plist(
        command="bernstein dashboard --headless",
        env={"BERNSTEIN_TELEGRAM_BOT_TOKEN": "t0k3n"},
        workdir="/Users/alice",
        path_env="/usr/local/bin:/usr/bin",
    )
    assert "<string>bernstein</string>" in out
    assert "<string>dashboard</string>" in out
    assert "<string>--headless</string>" in out
    assert "<string>/Users/alice</string>" in out
    assert "<string>/usr/local/bin:/usr/bin</string>" in out
    assert "<key>BERNSTEIN_TELEGRAM_BOT_TOKEN</key>" in out
    assert "<string>t0k3n</string>" in out
    assert "<key>RunAtLoad</key>" in out


def test_env_values_are_not_shell_expanded() -> None:
    # The host's `$PATH` must appear literally in the rendered unit,
    # never expanded by some shell. This guards against accidental leaks
    # when operators pass `--env SECRET=$SECRET`.
    literal = "$(whoami)`echo pwned`${HOME}"
    out = render_systemd_user_unit(
        command="bernstein dashboard --headless",
        env={"SECRET": literal},
        workdir="/srv/bernstein",
        path_env="/bin",
    )
    assert f'Environment="SECRET={literal}"' in out

    plist = render_launchd_plist(
        command="bernstein dashboard --headless",
        env={"SECRET": literal},
        workdir="/srv/bernstein",
        path_env="/bin",
    )
    assert f"<string>{literal}</string>" in plist
