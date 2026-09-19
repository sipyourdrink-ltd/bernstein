"""``bernstein adopt``: find the agent session to bring under governance (#5435).

An operator who already has a coding-agent session open should not have to
stop, run ``bernstein init``, pick an adapter by hand and restate the goal.
``bernstein adopt`` is meant to be the one-command path for that session.

This module is slice 1 of #5435: **detection only**. ``bernstein adopt
--dry-run`` reports which agent it detected, the evidence behind that answer,
and the files adoption would write -- and writes nothing. Running without
``--dry-run`` is refused with its own exit code rather than half-performed.
The files reported are ``bernstein init``'s own plan
(:func:`bernstein.cli.run_bootstrap.plan_init_writes`), and the tests hold
init to it in both directions: every planned path exists after a real init,
and init writes no path the plan does not name.

The detection table is data
---------------------------
Every signal is a row of :data:`DETECTION_PROBES`, one per (agent, signal).
Adding an agent or a signal is a new row, never a new branch. Each row names
where the signal is grounded, because a detector that guesses at an external
tool's internals reproduces the setup drift adoption exists to remove.

Two tiers of evidence
---------------------
* ``session`` -- an ancestor of this process runs the binary that agent's
  adapter spawns. That is direct evidence of the session being adopted.
* ``config`` -- the per-user configuration that agent keeps under the home
  directory exists. That proves the agent is set up on this machine, not
  that it is the one running now.

Project-level files are deliberately not probed: ``bernstein agents-md sync``
writes ``CLAUDE.md``, ``.cursor/rules/`` and ``.aider.conf.yml`` into every
synced repository, so in a Bernstein workspace they exist for every agent at
once and distinguish nothing.

Environment variables are not probed yet either. Nothing in this repository
records which variables each agent exports into the shells it spawns, and a
row naming a guessed variable would be a confident answer resting on nothing.
Once those names are established, each is one more row.

When two agents are detected
----------------------------
``--agent auto`` resolves at the strongest tier that matched anything. One
agent at that tier is selected; two or more are refused as ambiguous, named,
and the operator is asked for ``--agent``. A weaker tier never breaks a tie at
a stronger one. So a running Codex process wins over a Cursor configuration
directory, while two running agents -- or two configured agents with neither
running -- are refused rather than guessed between.

Why prefer the process over the file: configuration outlives sessions. A
machine that has ever had Cursor installed keeps ``~/.cursor`` indefinitely,
so refusing whenever a configuration file coexists with a running agent would
make adoption fail on most machines with more than one agent installed, which
defeats a one-command path. A tie within a tier is a genuinely ambiguous
signal, and adopting the wrong agent puts the wrong session under governance,
so that case is refused.

Known limits
------------
The process probe matches an ancestor's executable name and its first two
argv entries (interpreter and script). An agent launched through an
interpreter whose script is not named after the agent (``node .../cli.js``)
is invisible to it and can only be found at the ``config`` tier. Ancestry is
read with ``psutil`` when it is installed, from ``/proc`` on Linux otherwise,
and is empty elsewhere -- in which case only the ``config`` tier can match.

On the ``/proc`` path the name comes from ``stat``'s ``comm`` field, which the
kernel truncates to 15 characters; the full binary name is recovered from
``argv[0]`` in ``cmdline``. A process that has cleared its argv leaves only
the truncated name, so an agent binary longer than 15 characters is not
covered there. None of the five binaries in the table is that long today.

``~/.claude`` exists on nearly every machine that has run Claude Code, so
when no session is visible the ``config`` tier usually names ``claude``. That
makes the interpreter blind spot above the common case, not the edge case.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from collections.abc import Sequence

    from bernstein.cli.run_bootstrap import PlannedWrite

# Exit codes are operator contract (cli/AGENTS.md). 2 is left to Click's own
# usage errors, such as an unknown --agent value.
EXIT_OK: int = 0
EXIT_WRITE_NOT_IMPLEMENTED: int = 3
EXIT_NO_AGENT: int = 4
EXIT_AMBIGUOUS: int = 5

#: Adapter registry names ``--agent`` accepts besides ``auto``.
SUPPORTED_AGENTS: tuple[str, ...] = ("aider", "claude", "codex", "cursor", "opencode")

#: A running ancestor process: evidence of the session itself.
TIER_SESSION = "session"
#: Per-user configuration on disk: evidence the agent is set up here.
TIER_CONFIG = "config"
#: Strongest first. ``--agent auto`` resolves at the first tier that matched.
TIERS: tuple[str, ...] = (TIER_SESSION, TIER_CONFIG)

KIND_PROCESS = "process"
KIND_HOME_PATH = "home_path"

OUTCOME_SELECTED = "selected"
OUTCOME_EXPLICIT = "explicit"
OUTCOME_NONE = "none_detected"
OUTCOME_AMBIGUOUS = "ambiguous"

_EXIT_FOR_OUTCOME: dict[str, int] = {
    OUTCOME_SELECTED: EXIT_OK,
    OUTCOME_EXPLICIT: EXIT_OK,
    OUTCOME_NONE: EXIT_NO_AGENT,
    OUTCOME_AMBIGUOUS: EXIT_AMBIGUOUS,
}

#: Windows launcher suffixes stripped before comparing a process to a binary.
_LAUNCHER_SUFFIXES: tuple[str, ...] = (".exe", ".cmd", ".bat")

#: Ancestry is walked at most this far, so a pathological tree cannot stall.
_MAX_ANCESTORS = 32


@dataclass(frozen=True, slots=True)
class DetectionProbe:
    """One row of the detection table.

    Attributes:
        agent: Adapter registry name the probe is evidence for.
        kind: :data:`KIND_PROCESS` or :data:`KIND_HOME_PATH`.
        signal: The binary name, or the path relative to the home directory.
        tier: :data:`TIER_SESSION` or :data:`TIER_CONFIG`.
        grounded_in: Where the signal comes from, so a reviewer can check it.
    """

    agent: str
    kind: str
    signal: str
    tier: str
    grounded_in: str


DETECTION_PROBES: tuple[DetectionProbe, ...] = (
    DetectionProbe("aider", KIND_PROCESS, "aider", TIER_SESSION, "adapters/aider.py spawns `aider`"),
    DetectionProbe("claude", KIND_PROCESS, "claude", TIER_SESSION, "adapters/claude.py spawns `claude`"),
    DetectionProbe("codex", KIND_PROCESS, "codex", TIER_SESSION, "adapters/codex.py spawns `codex`"),
    DetectionProbe("cursor", KIND_PROCESS, "cursor-agent", TIER_SESSION, "adapters/cursor.py spawns `cursor-agent`"),
    DetectionProbe("opencode", KIND_PROCESS, "opencode", TIER_SESSION, "adapters/opencode.py spawns `opencode`"),
    DetectionProbe(
        "aider",
        KIND_HOME_PATH,
        ".aider.conf.yml",
        TIER_CONFIG,
        "aider's per-user config file (the format `bernstein agents-md sync` writes for aider)",
    ),
    DetectionProbe("claude", KIND_HOME_PATH, ".claude", TIER_CONFIG, "adapters/claude.py reads ~/.claude/projects"),
    DetectionProbe(
        "codex", KIND_HOME_PATH, ".codex/auth.json", TIER_CONFIG, "adapters/codex.py reads ~/.codex/auth.json"
    ),
    DetectionProbe("cursor", KIND_HOME_PATH, ".cursor", TIER_CONFIG, "adapters/cursor.py reads ~/.cursor/"),
    DetectionProbe(
        "opencode",
        KIND_HOME_PATH,
        ".config/opencode/opencode.jsonc",
        TIER_CONFIG,
        "adapters/opencode.py reads ~/.config/opencode/opencode.jsonc",
    ),
)


@dataclass(frozen=True, slots=True)
class ProcessInfo:
    """An ancestor process, as much of it as the platform lets us read.

    Attributes:
        name: Executable name.
        argv: Command line, when readable; empty otherwise.
    """

    name: str
    argv: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Evidence:
    """The result of evaluating one probe.

    Attributes:
        probe: The table row that was evaluated.
        matched: Whether the signal was observed.
        detail: What was observed, in words an operator can check.
    """

    probe: DetectionProbe
    matched: bool
    detail: str


@dataclass(frozen=True, slots=True)
class Detection:
    """Which agent adoption would target, and why.

    Attributes:
        outcome: One of the ``OUTCOME_*`` constants.
        selected: The chosen agent, or ``None`` when none or several matched.
        tier: The tier that decided the outcome; ``None`` when nothing did.
        candidates: The agents that matched at the deciding tier, sorted.
        evidence: Every probe that was evaluated, in table order.
    """

    outcome: str
    selected: str | None
    tier: str | None
    candidates: tuple[str, ...]
    evidence: tuple[Evidence, ...]


def _normalise(token: str) -> str:
    """Reduce a path or executable name to a lowercase basename without launcher suffix."""
    base = token.replace("\\", "/").rsplit("/", 1)[-1].strip().lower()
    for suffix in _LAUNCHER_SUFFIXES:
        if base.endswith(suffix):
            return base[: -len(suffix)]
    return base


def process_tokens(process: ProcessInfo) -> frozenset[str]:
    """The names a process can be matched on: its executable and first two argv entries."""
    tokens = {_normalise(process.name), *(_normalise(arg) for arg in process.argv[:2])}
    tokens.discard("")
    return frozenset(tokens)


def evaluate_probes(
    *,
    home: Path,
    ancestry: Sequence[ProcessInfo],
    probes: Sequence[DetectionProbe] = DETECTION_PROBES,
) -> tuple[Evidence, ...]:
    """Evaluate every probe against the observed ancestry and home directory.

    Pure over its inputs: the process tree and the home directory are passed
    in rather than read here, so detection is testable without real processes.

    Args:
        home: The home directory configuration probes resolve against.
        ancestry: Ancestor processes, nearest first.
        probes: The detection table to evaluate.

    Returns:
        One :class:`Evidence` per probe, in table order.

    Raises:
        ValueError: If a probe names a kind this module does not evaluate.
    """
    tokens = [process_tokens(process) for process in ancestry]
    evidence: list[Evidence] = []
    for probe in probes:
        if probe.kind == KIND_PROCESS:
            depth = next((index for index, seen in enumerate(tokens, start=1) if probe.signal in seen), None)
            matched = depth is not None
            detail = (
                f"ancestor process {depth} runs `{probe.signal}`"
                if matched
                else f"no ancestor process runs `{probe.signal}`"
            )
        elif probe.kind == KIND_HOME_PATH:
            matched = (home / probe.signal).exists()
            detail = f"~/{probe.signal} {'present' if matched else 'absent'}"
        else:
            raise ValueError(f"detection probe for {probe.agent!r} has unknown kind {probe.kind!r}")
        evidence.append(Evidence(probe=probe, matched=matched, detail=detail))
    return tuple(evidence)


def resolve_agent(evidence: Sequence[Evidence], *, requested: str) -> Detection:
    """Decide which agent to adopt from the evidence.

    A named agent is taken as given. ``auto`` resolves at the strongest tier
    that matched anything: one agent there is selected, several are refused as
    ambiguous, and a weaker tier is never consulted to break that tie.

    Args:
        evidence: The evaluated probes.
        requested: ``auto`` or an adapter name from :data:`SUPPORTED_AGENTS`.

    Returns:
        The detection, carrying the evidence it was decided from.
    """
    collected = tuple(evidence)
    if requested != "auto":
        return Detection(OUTCOME_EXPLICIT, requested, None, (requested,), collected)
    for tier in TIERS:
        agents = tuple(sorted({item.probe.agent for item in collected if item.matched and item.probe.tier == tier}))
        if len(agents) == 1:
            return Detection(OUTCOME_SELECTED, agents[0], tier, agents, collected)
        if agents:
            return Detection(OUTCOME_AMBIGUOUS, None, tier, agents, collected)
    return Detection(OUTCOME_NONE, None, None, (), collected)


def _ancestor_processes() -> tuple[ProcessInfo, ...]:
    """Read this process's ancestors, nearest first; empty when unreadable."""
    try:
        import psutil
    except ImportError:
        return _proc_ancestors()

    chain: list[ProcessInfo] = []
    try:
        current = psutil.Process().parent()
    except psutil.Error:
        return ()
    while current is not None and len(chain) < _MAX_ANCESTORS:
        try:
            name = current.name()
        except psutil.Error:
            name = ""
        try:
            argv = tuple(current.cmdline())
        except psutil.Error:
            argv = ()
        chain.append(ProcessInfo(name=name, argv=argv))
        try:
            current = current.parent()
        except psutil.Error:
            break
    return tuple(chain)


def _proc_ancestors() -> tuple[ProcessInfo, ...]:
    """Read ancestors from ``/proc`` without ``psutil``; empty off Linux."""
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return ()
    chain: list[ProcessInfo] = []
    pid = os.getppid()
    while pid > 1 and len(chain) < _MAX_ANCESTORS:
        entry = proc_root / str(pid)
        try:
            stat = (entry / "stat").read_text(encoding="utf-8", errors="replace")
            raw_argv = (entry / "cmdline").read_bytes()
        except OSError:
            break
        # ``comm`` sits in parentheses and may itself contain spaces or
        # parentheses, so the parent pid is read after the *last* ``)``.
        close = stat.rfind(")")
        fields = stat[close + 1 :].split()
        argv = tuple(part for part in raw_argv.decode("utf-8", errors="replace").split("\x00") if part)
        chain.append(ProcessInfo(name=stat[stat.find("(") + 1 : close], argv=argv))
        try:
            pid = int(fields[1])
        except (IndexError, ValueError):
            break
    return tuple(chain)


def _home() -> Path:
    """The home directory configuration probes resolve against."""
    return Path.home()


def _plan(root: Path) -> tuple[PlannedWrite, ...]:
    """``bernstein init``'s own write plan for *root* (imported lazily)."""
    from bernstein.cli.run_bootstrap import plan_init_writes

    return plan_init_writes(root)


def build_report(root: Path, detection: Detection, plan: Sequence[PlannedWrite]) -> dict[str, object]:
    """Project a detection and its write plan into the ``--json`` report."""
    return {
        "directory": root.as_posix(),
        "outcome": detection.outcome,
        "selected": detection.selected,
        "tier": detection.tier,
        "candidates": list(detection.candidates),
        "evidence": [
            {
                "agent": item.probe.agent,
                "kind": item.probe.kind,
                "signal": item.probe.signal,
                "tier": item.probe.tier,
                "matched": item.matched,
                "detail": item.detail,
            }
            for item in detection.evidence
        ],
        "would_write": [{"path": write.path, "action": write.action} for write in plan],
        "wrote": False,
    }


def _echo_text(root: Path, detection: Detection, plan: Sequence[PlannedWrite]) -> None:
    """Render a detection for a terminal. Errors go to stderr."""
    click.echo("bernstein adopt --dry-run: nothing will be written")
    if detection.outcome == OUTCOME_SELECTED:
        click.echo(f"Detected agent: {detection.selected} ({detection.tier} evidence)")
    elif detection.outcome == OUTCOME_EXPLICIT:
        click.echo(f"Agent: {detection.selected} (named with --agent; detection skipped)")
    if detection.evidence:
        click.echo("Evidence checked:")
        for item in detection.evidence:
            mark = "match" if item.matched else "-"
            click.echo(f"  {mark:<5}  {item.probe.agent:<8}  {item.probe.tier:<7}  {item.detail}")
    if plan:
        click.echo(f"Would write under {root}:")
        for write in plan:
            click.echo(f"  {write.action:<6}  {write.path}")
    if detection.outcome == OUTCOME_NONE:
        click.echo(
            "No agent detected: no ancestor process runs an agent binary and no agent "
            "configuration was found. Name the agent with --agent.",
            err=True,
        )
    elif detection.outcome == OUTCOME_AMBIGUOUS:
        click.echo(
            f"Ambiguous: {', '.join(detection.candidates)} all detected at the {detection.tier} tier. "
            "Name the agent with --agent.",
            err=True,
        )


@click.command("adopt")
@click.option(
    "--agent",
    "agent",
    type=click.Choice(("auto", *SUPPORTED_AGENTS)),
    default="auto",
    show_default=True,
    help="Agent to adopt. 'auto' detects it from the running session and per-user configuration.",
)
@click.option(
    "--dir",
    "target_dir",
    default=".",
    show_default=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="Workspace directory the adoption plan is computed for.",
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Report the detected agent and the files adoption would write; write nothing. Required for now.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the detection report as JSON.")
def adopt_cmd(agent: str, target_dir: Path, dry_run: bool, as_json: bool) -> None:
    """Detect the agent session to bring under governance.

    Only --dry-run is implemented: it prints which agent was detected, the
    evidence behind that answer, and the files adoption would write, and it
    writes nothing (#5435).

    \b
    Exit codes:
        0  agent detected, or named with --agent; plan printed
        2  usage error (for example an unknown --agent value)
        3  run without --dry-run: writing the adoption is not implemented yet
        4  --agent auto detected no agent
        5  --agent auto detected several agents at the strongest evidence tier
    """
    if not dry_run:
        click.echo(
            "bernstein adopt: only --dry-run is implemented. Writing the adoption (config, first task, "
            "doctor, receipt) lands in a later slice of #5435; nothing was written.",
            err=True,
        )
        sys.exit(EXIT_WRITE_NOT_IMPLEMENTED)

    root = target_dir.resolve()
    evidence = evaluate_probes(home=_home(), ancestry=_ancestor_processes()) if agent == "auto" else ()
    detection = resolve_agent(evidence, requested=agent)
    plan = _plan(root) if detection.selected is not None else ()

    if as_json:
        click.echo(json.dumps(build_report(root, detection, plan), indent=2, sort_keys=True))
    else:
        _echo_text(root, detection, plan)

    code = _EXIT_FOR_OUTCOME[detection.outcome]
    if code != EXIT_OK:
        sys.exit(code)
