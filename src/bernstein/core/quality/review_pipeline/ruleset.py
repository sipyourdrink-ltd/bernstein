"""Repo-level review ruleset: the standard a verdict was produced under.

A review drifts when its standard lives only in a prompt.  This module makes
the standard a loadable, hashable input:

* **raise rules** -- a defect class the reviewer must flag;
* **guard rules** -- a finding the reviewer must *not* raise, because an
  operator already rejected it as a false positive.

Guard rules are what makes an unattended reviewer tolerable: without them
every pass re-reports the same rejected finding and the fix pass chases it.

The rules live in ``.bernstein/review-rules.md`` by default, or wherever a
pipeline's ``rules:`` key points.  :attr:`ReviewRuleset.digest` is a sha256
over the *canonical* rule set -- sorted and de-duplicated -- so reordering the
file leaves the digest alone while editing a rule moves it.  That digest is
what a per-pass review receipt binds, so a verdict names its standard.

With no rules file the ruleset is empty, :meth:`to_prompt_section` returns the
empty string (the reviewer prompt is byte-identical to what shipped before
rulesets existed), and the digest is the digest of the empty set.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: Where a repository keeps its review rules unless a pipeline says otherwise.
DEFAULT_RULES_RELPATH = Path(".bernstein") / "review-rules.md"

#: Version stamped into the digest preimage. Bump only on a format change.
RULESET_DIGEST_VERSION = 1

RuleKind = Literal["raise", "guard"]

# A markdown heading whose text starts with "raise" / "guard" opens a section;
# any other heading closes the current one.
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(?P<title>.+?)\s*$")
_BULLET_RE = re.compile(r"^\s{0,3}[-*+]\s+(?P<text>.+?)\s*$")


class ReviewRulesetError(ValueError):
    """Raised when a ruleset explicitly named by a pipeline cannot be read."""


class RulesSpec(BaseModel):
    """The ``rules:`` mapping form in a pipeline YAML.

    Attributes:
        path: Rules file to load instead of the repository default.
        raise_rules: Extra raise rules declared inline (YAML key ``raise``).
        guard_rules: Extra guard rules declared inline (YAML key ``guard``).
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, populate_by_name=True)

    path: str | None = None
    raise_rules: list[str] = Field(default_factory=list[str], alias="raise")
    guard_rules: list[str] = Field(default_factory=list[str], alias="guard")


@dataclass(frozen=True)
class ReviewRule:
    """One rule the reviewer is held to.

    Attributes:
        kind: ``raise`` (must be flagged) or ``guard`` (must not be raised).
        text: The rule body, whitespace-stripped.
    """

    kind: RuleKind
    text: str


@dataclass(frozen=True)
class ReviewRuleset:
    """An ordered rule set plus the digest that names it.

    Attributes:
        rules: Rules in declaration order.
        source: Human-readable origin (a path, or ``""`` when none was found).
    """

    rules: tuple[ReviewRule, ...] = ()
    source: str = ""

    @property
    def raise_rules(self) -> tuple[ReviewRule, ...]:
        """Rules the reviewer must flag."""
        return tuple(r for r in self.rules if r.kind == "raise")

    @property
    def guard_rules(self) -> tuple[ReviewRule, ...]:
        """Rules the reviewer must not raise."""
        return tuple(r for r in self.rules if r.kind == "guard")

    @property
    def is_empty(self) -> bool:
        """True when no rule was declared anywhere."""
        return not self.rules

    @property
    def digest(self) -> str:
        """Content digest over the canonical (sorted, de-duplicated) rule set.

        Reordering the rules leaves this alone; editing one moves it.
        """
        payload = {
            "v": RULESET_DIGEST_VERSION,
            "guard": sorted({r.text for r in self.guard_rules}),
            "raise": sorted({r.text for r in self.raise_rules}),
        }
        canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_prompt_section(self) -> str:
        """Render the ruleset for a reviewer or fixer prompt.

        Returns the empty string for an empty ruleset so the prompt is
        unchanged when a repository ships no rules.
        """
        if self.is_empty:
            return ""
        lines: list[str] = ["", "## Review ruleset", "", f"Ruleset digest: {self.digest}", ""]
        if self.raise_rules:
            lines.append("### Raise - a review must flag these")
            lines.extend(f"- {r.text}" for r in self.raise_rules)
            lines.append("")
        if self.guard_rules:
            lines.append("### Guard - a review must not raise these (already rejected as false positives)")
            lines.extend(f"- {r.text}" for r in self.guard_rules)
            lines.append("")
        return "\n".join(lines)


#: The ruleset a repository with no rules file gets.
EMPTY_RULESET = ReviewRuleset()


def _section_kind(title: str) -> RuleKind | None:
    lowered = title.strip().lower()
    if lowered.startswith("raise"):
        return "raise"
    if lowered.startswith("guard"):
        return "guard"
    return None


def parse_ruleset(text: str, *, source: str = "") -> ReviewRuleset:
    """Parse a review-rules markdown document.

    Bullets under a heading whose text starts with ``raise`` / ``guard``
    become rules of that kind; everything else (prose, other headings) is
    ignored so the file stays readable as documentation.

    Args:
        text: Markdown source.
        source: Origin recorded on the ruleset for display.

    Returns:
        The parsed :class:`ReviewRuleset`.
    """
    rules: list[ReviewRule] = []
    kind: RuleKind | None = None
    for line in text.splitlines():
        heading = _HEADING_RE.match(line)
        if heading is not None:
            kind = _section_kind(heading.group("title"))
            continue
        if kind is None:
            continue
        bullet = _BULLET_RE.match(line)
        if bullet is not None:
            rules.append(ReviewRule(kind=kind, text=bullet.group("text").strip()))
    return ReviewRuleset(rules=tuple(rules), source=source)


def load_ruleset(
    *,
    repo_root: Path,
    rules: str | RulesSpec | None = None,
    base_dir: Path | None = None,
) -> ReviewRuleset:
    """Load the ruleset a review runs under.

    Args:
        repo_root: Repository root; the default rules path resolves against it.
        rules: A pipeline's ``rules:`` value -- a path string, a
            :class:`RulesSpec`, or ``None`` for the repository default.
        base_dir: Directory a relative ``rules:`` path resolves against
            (normally the pipeline YAML's directory); defaults to
            ``repo_root``.

    Returns:
        The loaded :class:`ReviewRuleset`; :data:`EMPTY_RULESET` when the
        repository ships no rules file.

    Raises:
        ReviewRulesetError: When a pipeline names a rules file that is absent
            or unreadable -- a typo there must not silently review against no
            standard.
    """
    spec = RulesSpec(path=rules) if isinstance(rules, str) else rules
    named = spec.path if spec is not None else None
    root = base_dir if base_dir is not None else repo_root
    path = (root / named) if named is not None else (repo_root / DEFAULT_RULES_RELPATH)

    if not path.is_file():
        if named is not None:
            raise ReviewRulesetError(f"rules file not found: {path}")
        loaded = EMPTY_RULESET
    else:
        try:
            loaded = parse_ruleset(path.read_text(encoding="utf-8"), source=str(path))
        except OSError as exc:
            raise ReviewRulesetError(f"cannot read rules file {path}: {exc}") from exc

    if spec is None or not (spec.raise_rules or spec.guard_rules):
        return loaded
    extra = tuple(ReviewRule(kind="raise", text=t.strip()) for t in spec.raise_rules)
    extra += tuple(ReviewRule(kind="guard", text=t.strip()) for t in spec.guard_rules)
    return ReviewRuleset(rules=loaded.rules + extra, source=loaded.source or "<pipeline rules>")
