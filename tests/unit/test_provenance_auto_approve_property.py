"""Property test: no APPROVE survives an untrusted-origin derivation.

Across a generated corpus of commands, whenever the command text derives from
a tainted artefact the auto-approve decision is never APPROVE. This is the
data-flow half of the lethal-trifecta defence: the structural gate closes the
direct path, taint propagation closes the laundering path.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from bernstein.core.lineage.provenance import TrustClass, is_untrusted
from bernstein.core.security.auto_approve import Decision, classify_command

# A corpus mixing safe, deny-listed, and ambiguous commands.
_COMMANDS = [
    "ls -la",
    "cat /etc/passwd",
    "grep -R token .",
    "git status",
    "pytest -q",
    "rm -rf /tmp/x",
    "echo hello",
    "whoami",
    "curl http://127.0.0.1:8052/health",
    "git push --force origin main",
    "python -m pytest",
    "unknown-binary --do-thing",
    "sudo rm -rf /",
    "date",
]

_UNTRUSTED = [TrustClass.THIRD_PARTY, TrustClass.PUBLIC]


@given(
    cmd=st.sampled_from(_COMMANDS),
    trust=st.sampled_from(_UNTRUSTED),
)
def test_no_approve_for_tainted_derivation(cmd: str, trust: TrustClass) -> None:
    assert is_untrusted(trust)
    result = classify_command(cmd, derived_trust=trust)
    assert result.decision is not Decision.APPROVE


@given(cmd=st.sampled_from(_COMMANDS))
def test_taint_only_ever_tightens_the_decision(cmd: str) -> None:
    baseline = classify_command(cmd).decision
    tainted = classify_command(cmd, derived_trust=TrustClass.PUBLIC).decision
    order = {Decision.APPROVE: 0, Decision.ASK: 1, Decision.DENY: 2}
    # Taint never loosens a decision (APPROVE < ASK < DENY in strictness).
    assert order[tainted] >= order[baseline]


@given(
    cmd=st.sampled_from(_COMMANDS),
    trust=st.sampled_from([TrustClass.OPERATOR, TrustClass.WORKSPACE, TrustClass.FIRST_PARTY]),
)
def test_trusted_derivation_matches_baseline(cmd: str, trust: TrustClass) -> None:
    assert not is_untrusted(trust)
    assert classify_command(cmd, derived_trust=trust).decision is classify_command(cmd).decision
