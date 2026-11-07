"""Shell writes into the control plane are denied however the path is spelled.

The deny rule for redirects into ``.sdd/`` used to match only the relative
form (``> .sdd/...``).  ``echo`` and ``printf`` are on the allow list, so the
same write spelled with an absolute path, or reached from an agent's worktree
(``.sdd/worktrees/<session>/``) through ``..`` segments, was auto-approved.
"""

from __future__ import annotations

import pytest

from bernstein.core.security.auto_approve import Decision, classify_command

_DENIED = [
    # Absolute targets.
    "echo '{}' > /home/dev/proj/.sdd/auth/agent_identities/x.json",
    "echo x >> /home/dev/proj/.sdd/auth/x",
    "printf '%s' x > \"/home/dev/proj/.sdd/auth/x.json\"",
    "echo x>/home/dev/proj/.sdd/auth/x",
    "echo x 1> /home/dev/proj/.sdd/auth/x",
    "echo x &> /home/dev/proj/.sdd/auth/x",
    "echo x >| /home/dev/proj/.sdd/auth/x",
    "echo x > $HOME/proj/.sdd/auth/x",
    "echo x > ${PWD}/../../auth/x",
    "echo x > /home/dev/proj/.bernstein/keys/agent-card.ed25519",
    "echo x > ./.sdd/auth/x",
    "awk 'BEGIN{print 1 > \"/home/dev/proj/.sdd/auth/x\"}'",
    # Parent-relative targets: from a worktree, ``../..`` is ``.sdd/``.
    "echo x > ../../auth/agent_identities/x.json",
    "echo x >> ../../../.sdd/auth/x",
    "printf x > 'sub/../../../auth/x'",
    # Write tools with the same targets.
    "echo x | tee /home/dev/proj/.sdd/auth/x",
    "echo x | tee -a ../../auth/x",
    "cp forged.json ../../auth/agent_identities/",
    "mv forged.json /home/dev/proj/.sdd/auth/agent_identities/",
    "install -m 600 forged.json /home/dev/proj/.sdd/auth/agent_identities/x.json",
    "sed -i 's/backend/manager/' ../../auth/agent_identities/x.json",
    # The relative forms the rule always covered.
    "echo x > .sdd/auth/x",
    "echo x >> .sdd/backlog/open/y.yaml",
]

_APPROVED = [
    "echo hello",
    "echo x > out.txt",
    "printf '%s\\n' a > build/out.txt",
    "ls ../",
    "grep -rn foo ../src",
    "pytest -q 2>&1",
    "echo x 2>/dev/null",
    "git diff main..HEAD",
    "echo a..b > notes.txt",
]


@pytest.mark.parametrize("command", _DENIED)
def test_control_plane_write_is_denied(command: str) -> None:
    result = classify_command(command)
    assert result.decision is Decision.DENY, (command, result)


@pytest.mark.parametrize("command", _APPROVED)
def test_ordinary_commands_keep_their_decision(command: str) -> None:
    result = classify_command(command)
    assert result.decision is Decision.APPROVE, (command, result)
