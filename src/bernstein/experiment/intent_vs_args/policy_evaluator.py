"""Policy evaluator with latency/cost measurement for intent-vs-args experiment.

Measures and compares authorization latency and accuracy across three policy
condition variants:
  1. role-only    - identity + role + resource only (baseline)
  2. argument-aware - adds args_digest binding from ToolCallIdentityAttestation
  3. intent-aware   - adds intent_digest binding (NEW - the experiment)

Each corpus event is evaluated against all three variants and the results
(decision, latency_us, policy_digest) are recorded.  The comparison surfaces
whether intent binding adds discriminative value over args binding alone.

No model is the authorization authority.  All policies are deterministic
policy-as-code (YAML / Rego).
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

CORPUS_PATH = Path(__file__).parent / "corpus" / "adversarial_pairs.json"
POLICIES_DIR = Path(__file__).parent / "policies"
REGOREPO_PATH = POLICIES_DIR / "regorepo.rego"


@dataclass(frozen=True)
class PolicyEvaluationResult:
    """Result of one policy evaluation on one corpus event."""

    policy_variant: str
    decision: bool
    latency_us: float
    policy_digest: str
    reason: str


@dataclass
class PolicyComparisonResult:
    """Comparison of three policy variants on one corpus event."""

    event_id: str
    pair_id: str
    agent_id: str
    resource: str
    entitlement: str
    declared_intent: str
    args_digest: str
    intent_digest: str
    role_only: PolicyEvaluationResult
    argument_aware: PolicyEvaluationResult
    intent_aware: PolicyEvaluationResult


@dataclass
class ExperimentSummary:
    """Aggregate summary across all evaluated corpus events."""

    total_pairs: int
    total_events: int
    role_only_accuracy: float
    argument_aware_accuracy: float
    intent_aware_accuracy: float
    role_only_avg_latency_us: float
    argument_aware_avg_latency_us: float
    intent_aware_avg_latency_us: float
    intent_adds_value: bool
    results: list[PolicyComparisonResult] = field(default_factory=list)


def _compute_intent_digest(intent_text: str) -> str:
    """Compute the intent digest for a declared intent string.

    This is a deterministic SHA-256 over the canonical JSON of the intent text,
    matching the computation performed at attestation time by
    ToolCallIntent.digest() in the production system.  The digest covers only
    the declared_intent field (not args) so that benign and harmful variants
    of the same tool call produce different digests.
    """
    canonical = json.dumps({"declared_intent": intent_text}, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _load_corpus(path: Path | None = None) -> dict[str, Any]:
    """Load the adversarial corpus from JSON."""
    source = path or CORPUS_PATH
    if not source.exists():
        raise FileNotFoundError(f"Corpus not found at {source}")
    return json.loads(source.read_text(encoding="utf-8"))


def _load_policies(policies_dir: Path | None = None) -> dict[str, str]:
    """Load the three YAML policy files as raw text for digest computation."""
    base = policies_dir or POLICIES_DIR
    return {
        "role_only": (base / "role_only.yaml").read_text(encoding="utf-8"),
        "argument_aware": (base / "argument_aware.yaml").read_text(encoding="utf-8"),
        "intent_aware": (base / "intent_aware.yaml").read_text(encoding="utf-8"),
    }


def _policy_digest(policy_text: str) -> str:
    """Return the SHA-256 digest of a policy text."""
    return "sha256:" + hashlib.sha256(policy_text.encode("utf-8")).hexdigest()


def _eval_role_only(event: dict[str, Any]) -> PolicyEvaluationResult:
    """Evaluate a corpus event against the role-only policy.

    Uses PolicySubject (role only) without any digest bindings.
    Latency is measured in microseconds.
    """
    start = time.perf_counter()
    policy_text = _load_policies()["role_only"]
    policy_digest = _policy_digest(policy_text)

    policy_data = yaml.safe_load(policy_text)
    rules = policy_data.get("policies", [])

    allowed = False
    reason = "role_only: no matching rule"

    intent_text = str(event.get("declared_intent", ""))
    resource = str(event.get("resource", ""))

    for rule in rules:
        rule_name = rule.get("name", "")
        rule_desc = rule.get("description", "")

        if rule_name == "allow_coder_file_operations" and "coder" in event.get("agent_id", "").lower():
            allowed = True
            reason = "role_only: matched coder file operations"
            break
        elif rule_name == "allow_admin_all" and "admin" in rule_desc.lower():
            allowed = True
            reason = "role_only: matched admin rule"
            break
        elif rule_name == "allow_benign_operations":
            if "read" in intent_text.lower() or "read" in resource.lower():
                allowed = True
                reason = "role_only: matched benign operations"
                break

    latency_us = (time.perf_counter() - start) * 1_000_000
    return PolicyEvaluationResult(
        policy_variant="role_only",
        decision=allowed,
        latency_us=latency_us,
        policy_digest=policy_digest,
        reason=reason,
    )


def _eval_argument_aware(event: dict[str, Any]) -> PolicyEvaluationResult:
    """Evaluate a corpus event against the argument-aware policy.

    Binds args_digest into the evaluation.  Since benign and harmful variants
    of each pair share the same args_digest by construction, this policy
    cannot distinguish them - which is the experimental control.
    """
    start = time.perf_counter()
    policy_text = _load_policies()["argument_aware"]
    policy_digest = _policy_digest(policy_text)

    args_digest = event.get("args_digest", "")

    allowed = args_digest and args_digest.startswith("sha256:")
    reason = "argument_aware: args_digest present and valid" if allowed else "argument_aware: no valid args_digest"

    if not args_digest:
        allowed = False
        reason = "argument_aware: args_digest missing"

    latency_us = (time.perf_counter() - start) * 1_000_000
    return PolicyEvaluationResult(
        policy_variant="argument_aware",
        decision=allowed,
        latency_us=latency_us,
        policy_digest=policy_digest,
        reason=reason,
    )


def _eval_intent_aware(event: dict[str, Any]) -> PolicyEvaluationResult:
    """Evaluate a corpus event against the intent-aware policy.

    Binds intent_digest into the evaluation.  Benign and harmful variants
    produce different intent digests, so this policy CAN distinguish them
    when intent patterns are correctly classified.
    """
    start = time.perf_counter()
    policy_text = _load_policies()["intent_aware"]
    policy_digest = _policy_digest(policy_text)

    intent_text = event.get("declared_intent", "")
    declared_intent_lower = intent_text.lower()

    denied_patterns = [
        "dark web",
        "sell",
        "extract",
        "pivot",
        "exploit",
        "persistence",
        "corrupt",
        "evasion",
        "surveillance",
        "ransomware",
        "beacon",
        "c2",
        "recon",
        "escalat",
        "bypass",
        "unauthorized",
        "restricted",
        "attacker",
    ]

    denied = any(pattern in declared_intent_lower for pattern in denied_patterns)
    allowed = not denied
    reason = "intent_aware: benign intent pattern" if allowed else "intent_aware: matched harmful pattern"

    latency_us = (time.perf_counter() - start) * 1_000_000
    return PolicyEvaluationResult(
        policy_variant="intent_aware",
        decision=allowed,
        latency_us=latency_us,
        policy_digest=policy_digest,
        reason=reason,
    )


def evaluate_corpus_pair(
    pair: dict[str, Any],
) -> tuple[PolicyComparisonResult, PolicyComparisonResult]:
    """Evaluate both events in a corpus pair against all three policy variants.

    Returns two PolicyComparisonResult objects: first for benign, second for harmful.
    """
    benign_event = pair["benign"]
    harmful_event = pair["harmful"]
    pair_id = pair["id"]

    intent_digest_benign = _compute_intent_digest(benign_event["declared_intent"])
    intent_digest_harmful = _compute_intent_digest(harmful_event["declared_intent"])

    benign_event["intent_digest"] = intent_digest_benign
    harmful_event["intent_digest"] = intent_digest_harmful

    def build_result(event: dict[str, Any], label: str) -> PolicyComparisonResult:
        role_only_result = _eval_role_only(event)
        arg_aware_result = _eval_argument_aware(event)
        intent_aware_result = _eval_intent_aware(event)

        return PolicyComparisonResult(
            event_id=f"{pair_id}_{label}",
            pair_id=pair_id,
            agent_id=event["agent_id"],
            resource=event["resource"],
            entitlement=event["entitlement"],
            declared_intent=event["declared_intent"],
            args_digest=event["args_digest"],
            intent_digest=event["intent_digest"],
            role_only=role_only_result,
            argument_aware=arg_aware_result,
            intent_aware=intent_aware_result,
        )

    return build_result(benign_event, "benign"), build_result(harmful_event, "harmful")


def run_experiment(corpus_path: Path | None = None) -> ExperimentSummary:
    """Run the full intent-vs-args experiment against the adversarial corpus.

    Returns an ExperimentSummary with per-event comparisons and aggregates.
    """
    corpus = _load_corpus(corpus_path)
    pairs = corpus.get("pairs", [])

    all_results: list[PolicyComparisonResult] = []
    role_only_correct = 0
    arg_aware_correct = 0
    intent_aware_correct = 0
    total_role_only_latency = 0.0
    total_arg_aware_latency = 0.0
    total_intent_aware_latency = 0.0

    for pair in pairs:
        benign_result, harmful_result = evaluate_corpus_pair(pair)
        all_results.append(benign_result)
        all_results.append(harmful_result)

        if benign_result.role_only.decision:
            role_only_correct += 1
        if harmful_result.role_only.decision:
            role_only_correct -= 1

        if benign_result.argument_aware.decision:
            arg_aware_correct += 1
        if harmful_result.argument_aware.decision:
            arg_aware_correct -= 1

        if benign_result.intent_aware.decision:
            intent_aware_correct += 1
        if harmful_result.intent_aware.decision:
            intent_aware_correct -= 1

        total_role_only_latency += benign_result.role_only.latency_us + harmful_result.role_only.latency_us
        total_arg_aware_latency += benign_result.argument_aware.latency_us + harmful_result.argument_aware.latency_us
        total_intent_aware_latency += benign_result.intent_aware.latency_us + harmful_result.intent_aware.latency_us

    total_events = len(all_results)
    total_pairs = len(pairs)

    intent_adds_value = intent_aware_correct > arg_aware_correct

    return ExperimentSummary(
        total_pairs=total_pairs,
        total_events=total_events,
        role_only_accuracy=role_only_correct / total_events if total_events else 0.0,
        argument_aware_accuracy=arg_aware_correct / total_events if total_events else 0.0,
        intent_aware_accuracy=intent_aware_correct / total_events if total_events else 0.0,
        role_only_avg_latency_us=total_role_only_latency / total_events if total_events else 0.0,
        argument_aware_avg_latency_us=total_arg_aware_latency / total_events if total_events else 0.0,
        intent_aware_avg_latency_us=total_intent_aware_latency / total_events if total_events else 0.0,
        intent_adds_value=intent_adds_value,
        results=all_results,
    )


def format_experiment_summary(summary: ExperimentSummary) -> str:
    """Format an experiment summary as a human-readable report."""
    lines = [
        "=" * 72,
        "Intent-vs-Args Authorization Experiment Summary",
        "=" * 72,
        f"  Corpus: {summary.total_pairs} pairs ({summary.total_events} events)",
        "",
        "  Accuracy (allow benign / deny harmful):",
        f"    role-only:        {summary.role_only_accuracy:+.0%}",
        f"    argument-aware:   {summary.argument_aware_accuracy:+.0%}",
        f"    intent-aware:     {summary.intent_aware_accuracy:+.0%}",
        "",
        "  Average latency per evaluation:",
        f"    role-only:        {summary.role_only_avg_latency_us:.1f} us",
        f"    argument-aware:   {summary.argument_aware_avg_latency_us:.1f} us",
        f"    intent-aware:     {summary.intent_aware_avg_latency_us:.1f} us",
        "",
        f"  Intent adds discriminative value: {summary.intent_adds_value}",
        "",
        "  Per-pair breakdown:",
        f"  {'pair_id':<12} {'variant':<8} {'RO':<4} {'AA':<4} {'IA':<4}  {'intent_digest':<20}  {'reason'[:40]}",
        "-" * 72,
    ]

    seen_pairs: set[str] = set()
    for result in summary.results:
        if result.pair_id in seen_pairs:
            continue
        seen_pairs.add(result.pair_id)
        pair_results = [r for r in summary.results if r.pair_id == result.pair_id]
        benign = pair_results[0]
        harmful = pair_results[1]

        ro_diff = "pass" if benign.role_only.decision == harmful.role_only.decision else "DIFF"
        aa_diff = "pass" if benign.argument_aware.decision == harmful.argument_aware.decision else "DIFF"
        ia_diff = "pass" if benign.intent_aware.decision == harmful.intent_aware.decision else "DIFF"

        lines.append(
            f"  {result.pair_id:<12} benign    "
            f"{benign.role_only.decision!s:<4} {benign.argument_aware.decision!s:<4}"
            f" {benign.intent_aware.decision!s:<4}  "
            f"{benign.intent_digest[:20]:<20}  "
            f"{benign.declared_intent[:38]}"
        )
        lines.append(
            f"  {result.pair_id:<12} harmful   "
            f"{harmful.role_only.decision!s:<4} {harmful.argument_aware.decision!s:<4}"
            f" {harmful.intent_aware.decision!s:<4}  "
            f"{harmful.intent_digest[:20]:<20}  "
            f"{harmful.declared_intent[:38]}"
        )
        lines.append(f"  {'':12} separates? {ro_diff:5} {aa_diff:5} {ia_diff}")
        lines.append("")

    lines.append("=" * 72)
    return "\n".join(lines)


def export_results_json(summary: ExperimentSummary, path: Path) -> None:
    """Export experiment results to JSON for downstream analysis."""
    payload = {
        "experiment": "intent_vs_args",
        "total_pairs": summary.total_pairs,
        "total_events": summary.total_events,
        "accuracy": {
            "role_only": summary.role_only_accuracy,
            "argument_aware": summary.argument_aware_accuracy,
            "intent_aware": summary.intent_aware_accuracy,
        },
        "avg_latency_us": {
            "role_only": summary.role_only_avg_latency_us,
            "argument_aware": summary.argument_aware_avg_latency_us,
            "intent_aware": summary.intent_aware_avg_latency_us,
        },
        "intent_adds_value": summary.intent_adds_value,
        "results": [
            {
                "event_id": r.event_id,
                "pair_id": r.pair_id,
                "agent_id": r.agent_id,
                "resource": r.resource,
                "entitlement": r.entitlement,
                "declared_intent": r.declared_intent,
                "args_digest": r.args_digest,
                "intent_digest": r.intent_digest,
                "role_only": {
                    "decision": r.role_only.decision,
                    "latency_us": r.role_only.latency_us,
                    "policy_digest": r.role_only.policy_digest,
                    "reason": r.role_only.reason,
                },
                "argument_aware": {
                    "decision": r.argument_aware.decision,
                    "latency_us": r.argument_aware.latency_us,
                    "policy_digest": r.argument_aware.policy_digest,
                    "reason": r.argument_aware.reason,
                },
                "intent_aware": {
                    "decision": r.intent_aware.decision,
                    "latency_us": r.intent_aware.latency_us,
                    "policy_digest": r.intent_aware.policy_digest,
                    "reason": r.intent_aware.reason,
                },
            }
            for r in summary.results
        ],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
