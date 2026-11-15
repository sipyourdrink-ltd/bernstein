"""Signed attribution and operator-qualified admission, without inference (#5038)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest

from bernstein.core.lineage.entry import ModelRef
from bernstein.core.routing.model_registry import load_registry_events, record_model_admission
from bernstein.core.security.audit_chain import AuditChainStore
from bernstein.eval.bench.admission_subject import AdmissionSubject
from bernstein.eval.bench.bundle import SubmissionBundle
from bernstein.eval.bench.model_admission import ModelAdmissionEvidence
from bernstein.eval.bench.runner import BenchRunner, MockReplayAdapter, StochasticMockReplayAdapter
from bernstein.eval.bench.signer import AgentCardSigner, StubSigner
from bernstein.eval.bench.suite import BenchSuite, BenchTask
from bernstein.eval.bench.verifier import BenchVerifier, verify_signature

if TYPE_CHECKING:
    from pathlib import Path


class ReceiptAdapter:
    """Hermetic replay derives pass/fail from a receipt, unlike the synthetic mock."""

    def run_task(self, task: BenchTask, scheduler_config: dict[str, Any]) -> dict[str, Any]:
        return {"outcome": "pass"}

    def score_task(self, task: BenchTask, receipt: dict[str, Any]) -> tuple[bool, float, dict[str, Any]]:
        passed = receipt["outcome"] == "pass"
        return passed, float(passed), {}


@pytest.fixture
def signer() -> AgentCardSigner:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    return AgentCardSigner(private, public)


@pytest.fixture
def evidence(signer: AgentCardSigner) -> ModelAdmissionEvidence:
    suite = BenchSuite(version="qualified-v1", tasks=[BenchTask("a", "A", ("check",), (), "code")])
    adapter = ReceiptAdapter()
    subject = AdmissionSubject(ModelRef("local", "model-a", version="revision-1"), ("code",))
    bundle = replace(BenchRunner(suite, adapter, {}).run(), admission_subject=subject)
    bundle = signer.sign(bundle)
    return ModelAdmissionEvidence(
        bundle, suite, adapter, {bundle.signer_fingerprint: signer.public_key_pem()}, ("code",)
    )


def qualify(evidence: ModelAdmissionEvidence, **overrides: Any) -> dict[str, Any]:
    return evidence.verify(
        **{
            "provider": "local",
            "model": "model-a",
            "version": "revision-1",
            "task_classes": ("code",),
            **overrides,
        }
    )


def test_subject_is_signed_and_roundtrips(evidence: ModelAdmissionEvidence) -> None:
    result = qualify(evidence)
    assert result["admission_subject"] == evidence.bundle.admission_subject.to_dict()
    loaded = SubmissionBundle.from_dict(evidence.bundle.to_dict())
    assert loaded.admission_subject == evidence.bundle.admission_subject
    assert loaded.bundle_hash() == evidence.bundle.bundle_hash()


@pytest.mark.parametrize(
    "changes",
    [
        {"provider": "local@other"},
        {"model_requested": "model-a@revision-1"},
        {"model_reported": "model-a@revision-1"},
        {"version": "revision-1@revision-2"},
        {"version": "*"},
    ],
)
def test_signed_subject_rejects_ambiguous_registry_components(changes: dict[str, Any]) -> None:
    fields = {"provider": "local", "model_requested": "model-a", "version": "revision-1", **changes}
    with pytest.raises(ValueError, match="registry"):
        AdmissionSubject(ModelRef(**fields), ("code",))


def test_stochastic_mock_cannot_qualify_real_signed_admission(
    evidence: ModelAdmissionEvidence,
    signer: AgentCardSigner,
) -> None:
    adapter = StochasticMockReplayAdapter(seed=0)
    bundle = replace(
        BenchRunner(evidence.suite, adapter, {}).run(), admission_subject=evidence.bundle.admission_subject
    )
    signed = signer.sign(bundle)
    mock_evidence = replace(evidence, bundle=signed, adapter=adapter)
    with pytest.raises(ValueError, match="synthetic"):
        qualify(mock_evidence)


@pytest.mark.parametrize("schema_version", [1, 2])
def test_legacy_payload_hashes_and_signature_remain_valid(
    evidence: ModelAdmissionEvidence, schema_version: int
) -> None:
    legacy = replace(evidence.bundle, schema_version=schema_version, admission_subject=None, signature="")
    legacy = StubSigner().sign(legacy)
    # Independent old-payload recipe: neither subject nor a v1 schema key existed.
    payload = {
        "suite_hash": legacy.suite_hash,
        "suite_version": legacy.suite_version,
        "submitted_at": legacy.submitted_at,
        "scheduler_config": legacy.scheduler_config,
        "task_results": [r.to_dict() for r in legacy.task_results],
    }
    if schema_version == 2:
        payload["schema_version"] = 2
    old_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert legacy.bundle_hash() == old_hash
    loaded = SubmissionBundle.from_dict(legacy.to_dict())
    assert verify_signature(loaded, allow_stub=True) == ""
    assert BenchVerifier(evidence.suite, evidence.adapter, allow_stub_signature=True).verify(loaded).passed
    with pytest.raises(ValueError, match="schema_version 3"):
        qualify(replace(evidence, bundle=loaded))


@pytest.mark.parametrize(
    "override",
    [
        {"provider": "other"},
        {"model": "other"},
        {"version": None},
        {"version": "other"},
        {"task_classes": ("review",)},
        {"task_classes": ("*",)},
    ],
)
def test_wrong_subject_or_scope_never_qualifies(evidence: ModelAdmissionEvidence, override: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="model_ref|task classes"):
        qualify(evidence, **override)


def test_bundle_cannot_qualify_its_own_task_scope(evidence: ModelAdmissionEvidence) -> None:
    with pytest.raises(ValueError, match="qualified suite"):
        qualify(replace(evidence, qualified_task_classes=("review",)))


def test_bundle_cannot_select_another_qualified_suite(evidence: ModelAdmissionEvidence) -> None:
    suite = replace(evidence.suite, tasks=[BenchTask("b", "B", (), ())])
    with pytest.raises(ValueError, match="verification"):
        qualify(replace(evidence, suite=suite))


@pytest.mark.parametrize("version", [0, 4, 99, "3", 3.0, True])
def test_unknown_or_malformed_bundle_schema_never_qualifies(evidence: ModelAdmissionEvidence, version: Any) -> None:
    bundle = replace(evidence.bundle, admission_subject=None, schema_version=version)
    with pytest.raises(ValueError, match="schema_version 3"):
        qualify(replace(evidence, bundle=bundle))


@pytest.mark.parametrize("version", [0, 2, "1", 1.0, True])
def test_unknown_subject_schema_is_rejected(evidence: ModelAdmissionEvidence, version: Any) -> None:
    raw = evidence.bundle.admission_subject.to_dict()
    raw["schema_version"] = version
    with pytest.raises(ValueError, match="schema_version"):
        AdmissionSubject.from_dict(raw)


def test_unchanged_cached_hash_does_not_hide_subject_mutation(evidence: ModelAdmissionEvidence) -> None:
    evidence.bundle.bundle_hash()
    evidence.bundle.admission_subject = AdmissionSubject(ModelRef("local", "other", version="revision-1"), ("code",))
    with pytest.raises(ValueError, match="Bundle hash mismatch"):
        qualify(evidence, model="other")


def test_rehashed_subject_tamper_still_fails_signature(evidence: ModelAdmissionEvidence) -> None:
    subject = AdmissionSubject(ModelRef("local", "other", version="revision-1"), ("code",))
    changed = replace(evidence.bundle, admission_subject=subject)
    with pytest.raises(ValueError, match="UNSIGNED"):
        qualify(replace(evidence, bundle=changed), model="other")


@pytest.mark.parametrize("failure", ["unsigned", "stub", "untrusted", "synthetic", "no_subject"])
def test_unqualified_provenance_is_refused(evidence: ModelAdmissionEvidence, failure: str) -> None:
    if failure == "unsigned":
        evidence = replace(evidence, bundle=replace(evidence.bundle, signature=""))
    elif failure == "stub":
        evidence = replace(evidence, bundle=StubSigner().sign(evidence.bundle))
    elif failure == "untrusted":
        evidence = replace(evidence, trusted_keys={})
    elif failure == "synthetic":
        evidence = replace(evidence, adapter=MockReplayAdapter())
    else:
        evidence = replace(evidence, bundle=replace(evidence.bundle, admission_subject=None))
    with pytest.raises(ValueError):
        qualify(evidence)


def test_replay_match_with_failed_task_is_not_admission(
    evidence: ModelAdmissionEvidence,
    signer: AgentCardSigner,
) -> None:
    class FailingAdapter(ReceiptAdapter):
        def score_task(self, task: BenchTask, receipt: dict[str, Any]) -> tuple[bool, float, dict[str, Any]]:
            return False, 0.0, {}

    adapter = FailingAdapter()
    bundle = replace(
        BenchRunner(evidence.suite, adapter, {}).run(), admission_subject=evidence.bundle.admission_subject
    )
    bundle = signer.sign(bundle)
    assert BenchVerifier(evidence.suite, adapter, evidence.trusted_keys).verify(bundle).passed
    with pytest.raises(ValueError, match="every.*task"):
        qualify(replace(evidence, bundle=bundle, adapter=adapter))


def test_qualified_admission_records_hash_bound_context_and_no_event_on_failure(
    evidence: ModelAdmissionEvidence,
    tmp_path: Path,
) -> None:
    chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
    args = dict(
        chain=chain,
        provider="local",
        model="model-a",
        version="revision-1",
        task_classes=("code",),
        admitted_by="operator@example.test",
        expires_at="2099-01-01T00:00:00.000000Z",
    )
    with pytest.raises(ValueError, match="verified evaluation evidence"):
        record_model_admission(**args, evidence_ref="opaque", require_verified_evidence=True)
    with pytest.raises(ValueError, match="model_ref"):
        record_model_admission(**{**args, "model": "other"}, evidence=evidence)
    assert load_registry_events(chain) == []
    event = record_model_admission(**args, evidence=evidence, require_verified_evidence=True)
    assert event.details["evidence_ref"] == "sha256:" + evidence.bundle.bundle_hash()
    assert event.details["verified_evidence"]["suite_hash"] == evidence.suite.suite_hash
    assert len(load_registry_events(chain)) == 1


def test_operator_cli_requires_evidence_and_records_verified_context(
    evidence: ModelAdmissionEvidence,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from click.testing import CliRunner

    from bernstein.cli.commands import model_cmd
    from bernstein.core.security.audit import AUDIT_KEY_ENV, load_or_create_audit_key

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(AUDIT_KEY_ENV, str(tmp_path / "audit.key"))
    monkeypatch.setattr("bernstein.eval.bench.bench_cli._get_suite", lambda name: evidence.suite)
    monkeypatch.setattr("bernstein.eval.bench.bench_cli._resolve_adapter", lambda suite: evidence.adapter)
    bundle_path = tmp_path / "bundle.json"
    evidence.bundle.save(bundle_path)
    public = tmp_path / "public.pem"
    public.write_bytes(evidence.trusted_keys[evidence.bundle.signer_fingerprint])
    args = [
        "admit",
        "local/model-a@revision-1",
        "--bundle",
        str(bundle_path),
        "--suite",
        "operator-selected",
        "--task-class",
        "code",
        "--qualified-task-class",
        "code",
        "--trusted-key",
        f"{evidence.bundle.signer_fingerprint}={public}",
        "--admitted-by",
        "operator@example.test",
        "--expires-at",
        "2099-01-01T00:00:00.000000Z",
    ]
    bad = [*args]
    bad[1] = "local/wrong@revision-1"
    result = CliRunner().invoke(model_cmd.model_group, bad)
    assert result.exit_code == 1, result.output
    assert not (tmp_path / ".sdd" / "audit").exists()
    assert not (tmp_path / "audit.key").exists()
    result = CliRunner().invoke(model_cmd.model_group, args)
    assert result.exit_code == 0, result.output
    chain = AuditChainStore(tmp_path / ".sdd" / "audit", key=load_or_create_audit_key())
    events = load_registry_events(chain)
    assert len(events) == 1
    assert events[0].details["verified_evidence"]["admission_subject"] == evidence.bundle.admission_subject.to_dict()


def test_bench_cli_binds_subject_before_signing(evidence: ModelAdmissionEvidence, tmp_path: Path) -> None:
    from click.testing import CliRunner

    from bernstein.eval.bench.bench_cli import bench_group

    subject_path = tmp_path / "subject.json"
    subject_path.write_text(json.dumps(evidence.bundle.admission_subject.to_dict()), encoding="utf-8")
    output = tmp_path / "bundle.json"
    result = CliRunner().invoke(
        bench_group,
        [
            "run",
            "golden-v1",
            "--stub-signer",
            "--out",
            str(output),
            "--admission-subject",
            str(subject_path),
        ],
    )
    assert result.exit_code == 0, result.output
    bundle = SubmissionBundle.load(output)
    assert bundle.admission_subject == evidence.bundle.admission_subject
    assert verify_signature(bundle, allow_stub=True) == ""
    # Signed attribution alone never promotes synthetic evaluation to admission evidence.
    with pytest.raises(ValueError):
        qualify(replace(evidence, bundle=bundle))


def test_holdout_and_duplicate_task_suites_are_not_qualified(evidence: ModelAdmissionEvidence) -> None:
    with pytest.raises(ValueError, match="holdout"):
        qualify(replace(evidence, suite=replace(evidence.suite, holdout_hash="a" * 64)))
    with pytest.raises(ValueError, match="unique task IDs"):
        qualify(replace(evidence, suite=replace(evidence.suite, tasks=evidence.suite.tasks * 2)))


def test_reported_model_substitution_is_not_an_alias_admission(
    evidence: ModelAdmissionEvidence,
    signer: AgentCardSigner,
) -> None:
    subject = AdmissionSubject(ModelRef("local", "model-a", "other", "revision-1"), ("code",))
    bundle = signer.sign(replace(evidence.bundle, admission_subject=subject))
    with pytest.raises(ValueError, match="different model identity"):
        qualify(replace(evidence, bundle=bundle))


def test_wrong_evidence_ref_never_appends(evidence: ModelAdmissionEvidence, tmp_path: Path) -> None:
    chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
    with pytest.raises(ValueError, match="evidence_ref"):
        record_model_admission(
            chain=chain,
            provider="local",
            model="model-a",
            version="revision-1",
            task_classes=("code",),
            admitted_by="operator@example.test",
            expires_at="2099-01-01T00:00:00.000000Z",
            evidence=evidence,
            evidence_ref="opaque",
            require_verified_evidence=True,
        )
    assert load_registry_events(chain) == []
