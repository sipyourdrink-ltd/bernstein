"""Lint for installed skill directories (issue #1720, track 1 minimum).

The default install and sync paths stay advisory for backward compatibility.
When their strict mode is enabled, ERROR findings block the operation while
WARNING findings remain advisory. Operators can also run lint by hand or via
CI to surface frontmatter typos, missing referenced files, sensitive patterns
the sanitiser flags, and body conventions (heading order, max length).

Checks performed:

- Frontmatter parses as YAML and matches :class:`SkillManifest` after a
  loose pre-filter (extra Claude-style keys like ``whenToUse`` are
  warnings, not errors, because authoring formats are still in flux).
- Every ``references`` / ``scripts`` / ``assets`` entry resolves to a
  real file under the matching bucket.
- The sanitiser (`core/skills/sanitizer.py`) reports zero stripped
  codepoints. Anything stripped is a high-signal sensitive-pattern leak.
- The body starts with an ``H1`` heading and is under 5 KB.
- The body carries no prompt-space risk phrasing (issue #2899, step 1):
  exfiltration-shaped instructions, credential-file asks, and
  approval-bypass wording are ERROR findings with code
  ``prompt-space-risk``. Each check is conjunctive (an egress verb next to
  a sensitive noun, a read verb next to a credential artifact) so ordinary
  skill vocabulary ("use environment variables for secrets") stays clean.

Output shape is a list of :class:`LintFinding` records. The caller renders
them and chooses the exit code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

import yaml
from pydantic import ValidationError

from bernstein.core.skills.manifest import SkillManifest
from bernstein.core.skills.sanitizer import strip_invisible_tags

#: Body cap: 5 KB before we warn. The RFC sets this as a soft cap so a
#: future skill that needs more context can still ship.
_MAX_BODY_BYTES: int = 5 * 1024

_VALID_BUCKETS: tuple[str, ...] = ("references", "scripts", "assets")

# ---------------------------------------------------------------------------
# Prompt-space risk patterns (issue #2899, step 1).
#
# A skill body is prompt-space code: it steers the agent that loads it. The
# three shapes below are the ones the trust gate needs to refuse before any
# third-party skill can be admitted. Every check is deliberately conjunctive
# per line - in-tree skills legitimately say "secrets", "POST", ".env" and
# "credential" in isolation, so single-keyword matching is unusable.
# ---------------------------------------------------------------------------

#: Egress verbs. A line is exfiltration-shaped only when one of these
#: co-occurs with a sensitive noun on the same line.
_EXFIL_EGRESS_RE: re.Pattern[str] = re.compile(
    r"(?i)\b(curl|wget|upload\w*|post\w*|send\w*|transmit\w*|exfiltrat\w*|forward\w*)\b"
)

#: Sensitive nouns for the exfiltration check. Bare "token" is excluded on
#: purpose - orchestration skills mention session/auth-header tokens
#: descriptively; only the compound forms are flagged.
_EXFIL_SENSITIVE_RE: re.Pattern[str] = re.compile(
    r"(?i)(\.env\b|\bcredentials?\b|\bsecrets?\b|\bapi[-_ ]?keys?\b"
    r"|\benvironment variables?\b|\bssh[-_ ]?keys?\b|\bprivate keys?\b"
    r"|\bid_rsa\b|\bauth tokens?\b|\baccess tokens?\b|\bapi tokens?\b)"
)

#: Read-verb next to a credential artifact (same line, verb first).
#: ``.env`` is handled separately below: file-management guidance
#: ("include .env in .gitignore", "copy .env.example to .env") is everyday
#: skill vocabulary, so only content-access verbs pair with ``.env``.
_CREDENTIAL_ASK_RE: re.Pattern[str] = re.compile(
    r"(?i)\b(read\w*|cat|open\w*|print\w*|dump\w*|copy|copies|copied|copying|extract\w*"
    r"|collect\w*|include|includes|included|including|fetch\w*|grab\w*|view\w*)\b"
    r"[^\n]{0,80}"
    r"(~?/?\.aws/credentials|\bid_rsa\b|\.ssh/|\b\.netrc\b|\bkeychain\b)"
)

#: Content-access verbs next to ``.env`` (same line, verb first).
_ENV_CONTENT_ASK_RE: re.Pattern[str] = re.compile(
    r"(?i)\b(read\w*|cat|print\w*|dump\w*|extract\w*)\b[^\n]{0,80}\.env\b"
)

#: Approval-bypass phrasings. Fixed shapes rather than keywords so that
#: "without sacrificing quality" or "skip flaky tests" stay clean.
_APPROVAL_BYPASS_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\b(ignore|disregard)\b[^\n]{0,40}\b(instructions?|system prompt|guardrails?|rules)\b"),
    re.compile(r"(?i)\bskip\w*\b[^\n]{0,40}\b(confirmation|approval|permission|consent)\b"),
    re.compile(r"(?i)\bdo not (ask|tell|inform|notify|mention)\b[^\n]{0,40}\b(user|operator|human|orchestrator)\b"),
)

#: "... without asking/approval" is a bypass instruction only when it is not
#: itself negated: "never merge without explicit approval" is a safeguard
#: (the retrieval pack ships exactly that line) and must stay clean.
_WITHOUT_APPROVAL_RE: re.Pattern[str] = re.compile(
    r"(?i)\bwithout\b[^\n]{0,15}\b(asking|prompting|notifying|informing|confirmation|approval|permission)\b"
)
_NEGATION_BEFORE_RE: re.Pattern[str] = re.compile(r"(?i)\b(never|not|don'?t|avoid)\b")

#: Clause boundaries for negation scoping. A negation only guards the
#: clause it appears in: "if tests are not green, push without asking"
#: is still a bypass - the "not" belongs to the previous clause.
_CLAUSE_SPLIT_RE: re.Pattern[str] = re.compile("[;,.:!?]|\u2014|\u2013| - ")


#: Markdown block openers that start a fresh logical line: list items,
#: ordered items, headings, quotes, table rows.
_BLOCK_START_RE: re.Pattern[str] = re.compile(r"^(?:[-*+]\s|\d+[.)]\s|#{1,6}\s|>|\|)")


#: Sequencing conjunctions start a new instruction, so a negation from the
#: previous step does not carry over: "do not upload secrets and then send
#: credentials" flags on "send". Plain coordination stays in scope:
#: "never upload or send secrets" is one safeguard.
_SEQUENCE_SPLIT_RE: re.Pattern[str] = re.compile(r"(?i)\b(?:and\s+then|then|after\s+that|afterwards?|next)\b")


def _negated_in_clause(line: str, at: int) -> bool:
    """Return True when the clause containing position ``at`` is negated.

    A negation only guards its own clause: "if tests are not green, push
    without asking" is still a bypass - the "not" belongs to the previous
    clause. "Never upload secrets" is a safeguard - the negation is local.
    Sequencing conjunctions also end a negation's scope (see
    :data:`_SEQUENCE_SPLIT_RE`).
    """
    clause_prefix = _CLAUSE_SPLIT_RE.split(line[:at])[-1]
    step_prefix = _SEQUENCE_SPLIT_RE.split(clause_prefix)[-1]
    return bool(_NEGATION_BEFORE_RE.search(step_prefix))


def _is_exfiltration(line: str) -> bool:
    """Return True when a line pairs a non-negated egress verb with a sensitive noun."""
    if not _EXFIL_SENSITIVE_RE.search(line):
        return False
    return any(not _negated_in_clause(line, match.start()) for match in _EXFIL_EGRESS_RE.finditer(line))


def _is_approval_bypass(line: str) -> bool:
    """Return True when a line carries approval-bypass phrasing."""
    if any(pattern.search(line) for pattern in _APPROVAL_BYPASS_RES):
        return True
    match = _WITHOUT_APPROVAL_RE.search(line)
    return bool(match) and not _negated_in_clause(line, match.start() if match else 0)


def _logical_lines(body: str) -> list[tuple[int, str]]:
    """Unwrap soft-wrapped markdown into logical lines for scanning.

    Consecutive plain-text lines render as one paragraph, so a phrase
    wrapped across raw lines must scan as one line - otherwise a line
    break in the middle of a watched phrase evades the checks. Lines that
    open a new block (list item, heading, quote, table row) start a fresh
    logical line; a wrapped continuation of a list item merges into the
    item. Code-fence content stays per-line. Returns ``(start_lineno,
    text)`` pairs with 1-based body line numbers.
    """
    out: list[tuple[int, str]] = []
    continues_previous = False
    in_fence = False
    for lineno, raw in enumerate(body.splitlines(), start=1):
        stripped = raw.strip()
        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
            continues_previous = False
            continue
        if not stripped:
            continues_previous = False
            continue
        if in_fence:
            out.append((lineno, stripped))
            continues_previous = False
            continue
        if _BLOCK_START_RE.match(stripped):
            out.append((lineno, stripped))
            # Headings, quotes and table rows are single-line blocks;
            # list items accept wrapped continuations.
            continues_previous = stripped[0] in "-*+" or stripped[0].isdigit()
            continue
        if continues_previous and out:
            start, text = out[-1]
            out[-1] = (start, f"{text} {stripped}")
        else:
            out.append((lineno, stripped))
        continues_previous = True
    return out


def _prompt_space_risk_findings(body: str, *, skill_name: str, skill_md: Path) -> list[LintFinding]:
    """Scan the body for instruction shapes the trust gate must refuse.

    Emits at most one finding per risk shape (the first offending line) so
    a hostile file produces a readable report rather than a wall of hits.
    """

    def _finding(label: str, lineno: int, line: str) -> LintFinding:
        snippet = line.strip()[:100]
        return LintFinding(
            skill_name=skill_name,
            severity=LintSeverity.ERROR,
            code="prompt-space-risk",
            message=f"{label} in body (body line {lineno}): {snippet!r}",
            path=skill_md,
        )

    findings: list[LintFinding] = []
    seen: set[str] = set()
    for lineno, line in _logical_lines(body):
        if "exfiltration" not in seen and _is_exfiltration(line):
            seen.add("exfiltration")
            findings.append(_finding("exfiltration-shaped instruction", lineno, line))
        if "credential-ask" not in seen and (_CREDENTIAL_ASK_RE.search(line) or _ENV_CONTENT_ASK_RE.search(line)):
            seen.add("credential-ask")
            findings.append(_finding("credential-file ask", lineno, line))
        if "approval-bypass" not in seen and _is_approval_bypass(line):
            seen.add("approval-bypass")
            findings.append(_finding("approval-bypass phrasing", lineno, line))
    return findings


class LintSeverity(StrEnum):
    """Severity of a lint finding.

    ``WARNING`` is the default. ``ERROR`` blocks install and sync only when
    the caller opts in to strict mode.
    """

    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True)
class LintFinding:
    """One advisory finding from :func:`lint_skill`."""

    skill_name: str
    severity: LintSeverity
    code: str
    message: str
    path: Path | None = None


def _split_skill_md(text: str) -> tuple[str, str] | None:
    """Re-implement the split locally to avoid importing the loader path.

    Returns ``None`` when the file lacks frontmatter at all.
    """
    lines = text.splitlines()
    if not lines or lines[0].rstrip() != "---":
        return None
    front: list[str] = []
    for idx in range(1, len(lines)):
        if lines[idx].rstrip() == "---":
            return ("\n".join(front), "\n".join(lines[idx + 1 :]).strip("\n"))
        front.append(lines[idx])
    return None


def _coerce_known_fields(raw: dict[str, Any]) -> dict[str, Any]:
    """Drop extra keys (e.g. ``whenToUse``) before strict validation.

    Strict :class:`SkillManifest` rejects unknown keys; we warn on them
    separately so an author who is mid-migration still gets useful
    feedback rather than a single fatal error.
    """
    allowed = set(SkillManifest.model_fields.keys())
    return {k: v for k, v in raw.items() if k in allowed}


def lint_skill(skill_dir: Path, *, skill_name: str | None = None) -> list[LintFinding]:
    """Run the advisory lint against one installed skill directory.

    Args:
        skill_dir: Directory containing ``SKILL.md``.
        skill_name: Override for reporting. Defaults to ``skill_dir.name``.

    Returns:
        Findings, possibly empty. An empty list means lint-clean.
    """
    name = skill_name or skill_dir.name
    findings: list[LintFinding] = []

    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        findings.append(
            LintFinding(
                skill_name=name,
                severity=LintSeverity.ERROR,
                code="missing-skill-md",
                message="SKILL.md is missing",
                path=skill_dir,
            )
        )
        return findings

    try:
        text = skill_md.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        findings.append(
            LintFinding(
                skill_name=name,
                severity=LintSeverity.ERROR,
                code="unreadable-skill-md",
                message=f"failed to read SKILL.md: {exc}",
                path=skill_md,
            )
        )
        return findings
    split = _split_skill_md(text)
    if split is None:
        findings.append(
            LintFinding(
                skill_name=name,
                severity=LintSeverity.ERROR,
                code="missing-frontmatter",
                message="SKILL.md has no YAML frontmatter",
                path=skill_md,
            )
        )
        return findings
    front_raw, body = split

    try:
        loaded = yaml.safe_load(front_raw)
    except yaml.YAMLError as exc:
        findings.append(
            LintFinding(
                skill_name=name,
                severity=LintSeverity.ERROR,
                code="yaml-error",
                message=f"frontmatter YAML failed to parse: {exc}",
                path=skill_md,
            )
        )
        return findings
    if not isinstance(loaded, dict):
        findings.append(
            LintFinding(
                skill_name=name,
                severity=LintSeverity.ERROR,
                code="frontmatter-shape",
                message="frontmatter must be a YAML mapping",
                path=skill_md,
            )
        )
        return findings
    raw_dict = cast("dict[str, Any]", loaded)

    extra_keys = sorted(set(raw_dict.keys()) - set(SkillManifest.model_fields.keys()))
    for key in extra_keys:
        findings.append(
            LintFinding(
                skill_name=name,
                severity=LintSeverity.WARNING,
                code="extra-key",
                message=f"frontmatter contains unknown key {key!r}",
                path=skill_md,
            )
        )

    try:
        manifest = SkillManifest.model_validate(_coerce_known_fields(raw_dict))
    except ValidationError as exc:
        findings.append(
            LintFinding(
                skill_name=name,
                severity=LintSeverity.ERROR,
                code="invalid-manifest",
                message=f"manifest failed validation: {exc}",
                path=skill_md,
            )
        )
        return findings

    # Anchor containment to the resolved skill root so a symlinked
    # bucket (``references -> /tmp/elsewhere``) cannot smuggle host
    # files past the lint. The bucket directories themselves are not
    # resolved; we build candidates from the unresolved path and check
    # them against the resolved skill root.
    skill_root = skill_dir.resolve()
    for bucket in _VALID_BUCKETS:
        bucket_root = skill_root / bucket
        for filename in getattr(manifest, bucket):
            rel = Path(filename)
            if rel.is_absolute() or ".." in rel.parts:
                findings.append(
                    LintFinding(
                        skill_name=name,
                        severity=LintSeverity.ERROR,
                        code="unsafe-reference-path",
                        message=(
                            f"manifest declares unsafe {bucket} path {filename!r}; "
                            "absolute paths and parent traversal are not allowed"
                        ),
                        path=skill_md,
                    )
                )
                continue
            candidate = (skill_dir / bucket / rel).resolve()
            if not candidate.is_relative_to(bucket_root):
                findings.append(
                    LintFinding(
                        skill_name=name,
                        severity=LintSeverity.ERROR,
                        code="unsafe-reference-path",
                        message=(f"manifest {bucket} path {filename!r} escapes the {bucket}/ root"),
                        path=skill_md,
                    )
                )
                continue
            if not candidate.is_file():
                findings.append(
                    LintFinding(
                        skill_name=name,
                        severity=LintSeverity.ERROR,
                        code="missing-reference",
                        message=f"manifest declares {bucket}/{filename} but the file is missing",
                        path=candidate,
                    )
                )

    _cleaned, stripped = strip_invisible_tags(text)
    if stripped > 0:
        findings.append(
            LintFinding(
                skill_name=name,
                severity=LintSeverity.ERROR,
                code="sensitive-pattern",
                message=(
                    f"sanitiser stripped {stripped} invisible Unicode codepoint(s); "
                    "this skill carries a likely prompt-injection payload"
                ),
                path=skill_md,
            )
        )

    body_bytes = len(body.encode("utf-8"))
    if body_bytes > _MAX_BODY_BYTES:
        findings.append(
            LintFinding(
                skill_name=name,
                severity=LintSeverity.WARNING,
                code="body-too-large",
                message=(f"body is {body_bytes} bytes; recommended max is {_MAX_BODY_BYTES}"),
                path=skill_md,
            )
        )

    findings.extend(_prompt_space_risk_findings(body, skill_name=name, skill_md=skill_md))

    body_lines = [line for line in body.splitlines() if line.strip()]
    if body_lines and not body_lines[0].lstrip().startswith("#"):
        findings.append(
            LintFinding(
                skill_name=name,
                severity=LintSeverity.WARNING,
                code="missing-h1",
                message="body should start with an H1 heading",
                path=skill_md,
            )
        )

    return findings


__all__ = ["LintFinding", "LintSeverity", "lint_skill"]
