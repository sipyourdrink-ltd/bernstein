"""Declarative executor admission policy for spawns (issue #4907).

``bernstein.yaml`` already *selects* an adapter, a model and an endpoint
profile.  Nothing in it lets an operator declare which executors a
repository may use at all, so a mis-pinned role or a stray
``--adapter`` silently runs on an executor the operator never approved.

This module adds that declaration.  An ``admission:`` block lists
ordered rules; every spawn is reduced to an :class:`AdmissionSubject`
(role, adapter, model, endpoint, sandbox tier, task type) and evaluated
against them *before any agent process starts*:

* every ``effect: deny`` rule is evaluated first, in declaration order -
  an explicit deny can never be re-opened by a later ``allow``;
* then the first matching ``effect: allow`` rule admits;
* a subject matching no allow rule is **refused** - the policy is fail
  closed, so widening it is an edit to the config rather than an
  omission in it.

Every axis matches by shell glob (:func:`fnmatch.fnmatchcase`,
case-sensitive); a rule that omits an axis does not constrain it.
Evaluation is a pure function of ``(policy, subject)``, so a replay of
the same config and the same spawn identity reproduces the same rule id.

Usage::

    from bernstein.core.security.executor_admission import (
        AdmissionPolicy,
        AdmissionSubject,
    )

    policy = AdmissionPolicy.load(workdir)
    if policy is not None:
        decision = policy.evaluate(subject)
        if not decision.allowed:
            raise PermissionError(decision.reason)
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Any, Literal, cast

import yaml

from bernstein.core.security.capability_matrix import EnforcementMode

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "ADMISSION_AXES",
    "REASON_NO_MATCH",
    "AdmissionDecision",
    "AdmissionPolicy",
    "AdmissionPolicyError",
    "AdmissionRule",
    "AdmissionSubject",
]

#: Rule key -> subject attribute.  The single source of truth for which
#: axes exist: parsing, matching and the ``admission check`` table all
#: read it, so adding an axis cannot leave one of them behind.
ADMISSION_AXES: dict[str, str] = {
    "roles": "role",
    "adapters": "adapter",
    "models": "model",
    "endpoints": "endpoint",
    "sandboxes": "sandbox",
    "task_types": "task_type",
}

_RULE_KEYS: frozenset[str] = frozenset({"id", "effect", *ADMISSION_AXES})
_POLICY_KEYS: frozenset[str] = frozenset({"mode", "rules"})

#: Reason recorded when the policy is declared but nothing admits the
#: subject.  Constant so audit consumers can match on it.
REASON_NO_MATCH = "no admission rule matched"

#: Effect recorded on a decision that matched no rule at all.
_EFFECT_NONE = "none"


class AdmissionPolicyError(ValueError):
    """Raised when an ``admission:`` block is malformed.

    Carries an actionable message naming the offending key or rule so a
    typo surfaces at the config boundary rather than as a refused spawn
    halfway through a run.
    """


@dataclass(frozen=True, slots=True)
class AdmissionSubject:
    """The executor identity of one spawn, as the policy sees it.

    Attributes:
        role: Agent role being spawned.
        adapter: Adapter that will serve the spawn.
        model: Model the adapter will run.
        endpoint: Endpoint base URL, or ``""`` when the adapter uses its
            own built-in endpoint.
        sandbox: Sandbox tier - the bound sandbox backend's name when one
            is configured, otherwise the resolved isolation mode
            (``container`` / ``worktree`` / ``none``).
        task_type: The task's ``TaskType`` value.
    """

    role: str
    adapter: str
    model: str
    endpoint: str = ""
    sandbox: str = "none"
    task_type: str = "standard"

    def as_dict(self) -> dict[str, str]:
        """Return the subject as a plain mapping for records and tables."""
        return {axis: getattr(self, axis) for axis in ADMISSION_AXES.values()}


@dataclass(frozen=True, slots=True)
class AdmissionRule:
    """One ordered allow/deny rule.

    An axis left empty does not constrain that axis; a populated axis
    matches when any of its glob patterns matches the subject's value.

    Attributes:
        rule_id: Operator-chosen identifier, unique within the policy.
            It is what a decision record names, so it must be stable.
        effect: ``allow`` or ``deny``.
        roles: Role patterns.
        adapters: Adapter-name patterns.
        models: Model patterns.
        endpoints: Endpoint base-URL patterns.
        sandboxes: Sandbox-tier patterns.
        task_types: Task-type patterns.
    """

    rule_id: str
    effect: Literal["allow", "deny"]
    roles: tuple[str, ...] = ()
    adapters: tuple[str, ...] = ()
    models: tuple[str, ...] = ()
    endpoints: tuple[str, ...] = ()
    sandboxes: tuple[str, ...] = ()
    task_types: tuple[str, ...] = ()

    def matches(self, subject: AdmissionSubject) -> bool:
        """Return True when every constrained axis matches *subject*."""
        for key, attr in ADMISSION_AXES.items():
            patterns: tuple[str, ...] = getattr(self, key)
            if not patterns:
                continue
            value = getattr(subject, attr)
            if not any(fnmatchcase(value, pattern) for pattern in patterns):
                return False
        return True


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    """Outcome of evaluating one subject against the policy.

    Attributes:
        allowed: Whether the spawn may proceed under the active mode.
        rule_id: Rule that decided, or ``""`` when nothing matched.
        effect: ``allow``, ``deny``, or ``none`` when nothing matched.
        reason: Human-readable explanation.
        subject: The evaluated subject.
        mode: Enforcement mode used for this evaluation.
    """

    allowed: bool
    rule_id: str
    effect: str
    reason: str
    subject: AdmissionSubject
    mode: EnforcementMode

    def as_record(self) -> dict[str, Any]:
        """Return the decision as the JSON record persisted per spawn."""
        return {
            "allowed": self.allowed,
            "rule_id": self.rule_id,
            "effect": self.effect,
            "reason": self.reason,
            "mode": self.mode.value,
            "subject": self.subject.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class AdmissionPolicy:
    """An ordered, fail-closed set of :class:`AdmissionRule`.

    Attributes:
        rules: Rules in declaration order.
        mode: ``enforce`` refuses; ``warn`` admits but records the
            refusing rule; ``off`` admits and records the evaluation.
    """

    rules: tuple[AdmissionRule, ...] = ()
    mode: EnforcementMode = EnforcementMode.ENFORCE

    def evaluate(self, subject: AdmissionSubject) -> AdmissionDecision:
        """Evaluate *subject*: deny rules first, then allow, then refuse.

        Args:
            subject: The spawn's executor identity.

        Returns:
            An :class:`AdmissionDecision`.  In ``warn`` / ``off`` mode a
            refusal still reports the rule that refused, so an operator
            can stage a policy before turning it on.
        """
        for rule in self.rules:
            if rule.effect == "deny" and rule.matches(subject):
                return self._refuse(
                    subject,
                    rule_id=rule.rule_id,
                    effect="deny",
                    reason=f"admission denied by rule {rule.rule_id!r}",
                )
        for rule in self.rules:
            if rule.effect == "allow" and rule.matches(subject):
                return AdmissionDecision(
                    allowed=True,
                    rule_id=rule.rule_id,
                    effect="allow",
                    reason=f"admitted by rule {rule.rule_id!r}",
                    subject=subject,
                    mode=self.mode,
                )
        return self._refuse(
            subject,
            rule_id="",
            effect=_EFFECT_NONE,
            reason=REASON_NO_MATCH,
        )

    def _refuse(
        self,
        subject: AdmissionSubject,
        *,
        rule_id: str,
        effect: str,
        reason: str,
    ) -> AdmissionDecision:
        """Build a refusal, downgraded to an admit in warn/off mode."""
        if self.mode is EnforcementMode.ENFORCE:
            return AdmissionDecision(
                allowed=False,
                rule_id=rule_id,
                effect=effect,
                reason=reason,
                subject=subject,
                mode=self.mode,
            )
        suffix = "warn-only" if self.mode is EnforcementMode.WARN else "enforcement off"
        return AdmissionDecision(
            allowed=True,
            rule_id=rule_id,
            effect=effect,
            reason=f"{reason} ({suffix})",
            subject=subject,
            mode=self.mode,
        )

    @classmethod
    def from_mapping(cls, raw: object) -> AdmissionPolicy:
        """Build a policy from the raw ``admission:`` mapping.

        Args:
            raw: The YAML value of the ``admission`` key.

        Returns:
            The parsed policy.

        Raises:
            AdmissionPolicyError: On any shape or key error, naming the
                offending key or rule.
        """
        if not isinstance(raw, dict):
            raise AdmissionPolicyError(f"admission must be a mapping, got: {type(raw).__name__}")
        block = cast("dict[str, object]", raw)
        unknown = sorted({str(key) for key in block} - _POLICY_KEYS)
        if unknown:
            raise AdmissionPolicyError(
                f"admission has unknown keys: {', '.join(unknown)} (known keys: {', '.join(sorted(_POLICY_KEYS))})"
            )
        mode = _parse_mode(block.get("mode"))
        raw_rules: object = block.get("rules", [])
        if raw_rules is None:
            raw_rules = []
        if not isinstance(raw_rules, list):
            raise AdmissionPolicyError(f"admission.rules must be a list, got: {type(raw_rules).__name__}")
        entries = cast("list[object]", raw_rules)
        rules: list[AdmissionRule] = []
        seen: set[str] = set()
        for index, entry in enumerate(entries):
            rule = _parse_rule(index, entry)
            if rule.rule_id in seen:
                # Two rules under one id would make a decision record
                # ambiguous, so replay could not reproduce which rule
                # decided.  Reject at the boundary instead.
                raise AdmissionPolicyError(f"admission.rules[{index}]: duplicate rule id {rule.rule_id!r}")
            seen.add(rule.rule_id)
            rules.append(rule)
        return cls(rules=tuple(rules), mode=mode)

    @classmethod
    def load(cls, workdir: Path, *, config_name: str = "bernstein.yaml") -> AdmissionPolicy | None:
        """Load the policy declared in ``<workdir>/<config_name>``.

        The file is re-read per call rather than cached, so an operator
        edit takes effect on the next spawn and a replay reads the same
        bytes the run read.

        Args:
            workdir: Repository root holding the config file.
            config_name: Config file name, for callers that keep the
                seed under a different name.

        Returns:
            The parsed policy, or ``None`` when the file is absent or
            declares no ``admission:`` block.

        Raises:
            AdmissionPolicyError: When the file is unreadable or the
                block is malformed - a broken policy must refuse, never
                silently disable the gate.
        """
        path = workdir / config_name
        if not path.is_file():
            return None
        try:
            data: object = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise AdmissionPolicyError(f"could not read admission policy from {path}: {exc}") from exc
        if not isinstance(data, dict):
            return None
        document = cast("dict[str, object]", data)
        if "admission" not in document:
            return None
        return cls.from_mapping(document["admission"])


def _parse_mode(raw: object) -> EnforcementMode:
    """Coerce the ``mode`` key into an :class:`EnforcementMode`."""
    if raw is None:
        return EnforcementMode.ENFORCE
    if not isinstance(raw, str):
        raise AdmissionPolicyError(f"admission.mode must be a string, got: {type(raw).__name__}")
    try:
        return EnforcementMode(raw)
    except ValueError as exc:
        known = ", ".join(m.value for m in EnforcementMode)
        raise AdmissionPolicyError(f"admission.mode must be one of: {known} (got {raw!r})") from exc


def _parse_rule(index: int, entry: object) -> AdmissionRule:
    """Parse one ``admission.rules`` entry into an :class:`AdmissionRule`."""
    where = f"admission.rules[{index}]"
    if not isinstance(entry, dict):
        raise AdmissionPolicyError(f"{where} must be a mapping, got: {type(entry).__name__}")
    fields = cast("dict[str, object]", entry)
    unknown = sorted({str(key) for key in fields} - _RULE_KEYS)
    if unknown:
        raise AdmissionPolicyError(
            f"{where} has unknown keys: {', '.join(unknown)} (known keys: {', '.join(sorted(_RULE_KEYS))})"
        )
    rule_id = fields.get("id")
    if not isinstance(rule_id, str) or not rule_id:
        raise AdmissionPolicyError(f"{where}.id must be a non-empty string")
    effect = fields.get("effect")
    if effect not in ("allow", "deny"):
        raise AdmissionPolicyError(f"{where}.effect must be 'allow' or 'deny' (got {effect!r})")
    axes = {key: _parse_patterns(f"{where}.{key}", fields.get(key)) for key in ADMISSION_AXES}
    if effect == "allow" and not any(axes.values()):
        # An allow rule with no constrained axis admits every possible
        # subject, which quietly turns a fail-closed policy into an
        # open one.  Operators who want that write ``adapters: ["*"]``.
        raise AdmissionPolicyError(f"{where}: an allow rule must constrain at least one axis")
    return AdmissionRule(rule_id=rule_id, effect=effect, **axes)


def _parse_patterns(where: str, raw: object) -> tuple[str, ...]:
    """Coerce one axis value into a tuple of non-empty glob patterns."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise AdmissionPolicyError(f"{where} must be a list of patterns, got: {type(raw).__name__}")
    patterns: list[str] = []
    for item in cast("list[object]", raw):
        if not isinstance(item, str) or not item:
            raise AdmissionPolicyError(f"{where} entries must be non-empty strings (got {item!r})")
        patterns.append(item)
    return tuple(patterns)
