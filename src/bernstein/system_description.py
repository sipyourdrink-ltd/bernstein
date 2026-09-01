"""How Bernstein describes ITSELF, in one place.

Three code paths described a narrower system than the one that ships: the EU AI
Act evidence package, the ``/.well-known/`` agent payload, and the Cursor rules
that land in a user's editor. All three said "orchestrator for CLI coding
agents", while ``README.md``, ``AGENTS.md``, ``pyproject.toml``, ``action.yml``
and ``agents.txt`` had already been updated to describe the governance layer
(#5004).

The mismatch was not only wording. The recording, gating and verification path
never inspects what an agent produces - a diff, a report, a dataset and an
evidence pack travel it identically - so naming CLI coding agents as *the*
subject understates the code it describes. One of the three lands verbatim in
the artifact handed to an assessor.

**CLI coding agents stay named.** They are the out-of-the-box path, and dropping
them would swap one inaccuracy for another. The claim being fixed is that they
are the only subject, not that they are a subject.

These live in code rather than in docs, which is why a docs pass did not reach
them and why they drifted in the first place. Kept here so the three read from
one string: ``test_system_description.py`` pins that, and that the wording stays
consistent with ``AGENTS.md``.
"""

from __future__ import annotations

from typing import Final

#: What the system IS. Lands in the evidence package and at `/.well-known/`.
SYSTEM_DESCRIPTION: Final[str] = (
    "The governance layer for AI agents. A deterministic scheduler - no model in the "
    "coordination loop - runs agents in parallel, gates what they produce, and records "
    "every step, so a run can be verified after the fact, offline, from the artifacts "
    "alone. CLI coding agents (Claude Code, Codex, Gemini CLI, and 40+ more) work out of "
    "the box, and the same layer governs any agent workload. It is governance "
    "middleware, not a decision-making AI system."
)

#: What it is FOR. Covers the deliverable classes the layer governs, not code alone.
INTENDED_USE: Final[str] = (
    "Governing agent workloads under human supervision: running agents in parallel, "
    "gating what they produce, and recording every step for after-the-fact verification. "
    "The deliverable can be a code diff, a research report, a dataset, or an audit "
    "evidence pack - the recording and gating path treats them identically."
)

#: How it is deployed. The evidence package's default `deployment_context`.
DEPLOYMENT_CONTEXT: Final[str] = "Self-hosted governance layer for AI agent workloads"

__all__ = ["DEPLOYMENT_CONTEXT", "INTENDED_USE", "SYSTEM_DESCRIPTION"]
