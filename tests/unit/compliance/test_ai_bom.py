"""Unit tests for the AI Bill of Materials module.

Covers ≥45 unit tests, ≥15 Hypothesis property tests, and a snapshot
test for the CycloneDX-AI JSON shape. The bar is "MAXIMUM density" per
issue #1371.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from bernstein.core.compliance.ai_bom import (
    AIBOM,
    BOM_SCHEMA_URL,
    BOM_SCHEMA_VERSION,
    SUPPORTED_FORMATS,
    AdapterEntry,
    BOMError,
    DataSourceEntry,
    ModelEntry,
    PromptEntry,
    ToolEntry,
    bom_content_hash,
    encode_bom,
    generate_bom,
    verify_bom,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode()).hexdigest()


def _root_hash() -> str:
    return _sha("lineage-root")


def _minimal_snapshot(**overrides: Any) -> dict[str, Any]:
    snap: dict[str, Any] = {
        "run_id": "20260518-101010",
        "started_at": "2026-05-18T10:10:10Z",
        "finished_at": "2026-05-18T10:11:00Z",
        "lineage_root_hash": _root_hash(),
        "bernstein_version": "2.1.0",
        "models": [],
        "prompts": [],
        "adapters": [],
        "tools": [],
        "data_sources": [],
    }
    snap.update(overrides)
    return snap


def _full_snapshot() -> dict[str, Any]:
    return _minimal_snapshot(
        models=[
            {
                "name": "claude-3-7-sonnet",
                "provider": "anthropic",
                "version": "2026-02-15",
                "sha256": _sha("model-sonnet"),
                "invocation_count": 3,
            },
            {
                "name": "gpt-4o",
                "provider": "openai",
                "version": "2026-04-09",
                "sha256": _sha("model-gpt-4o"),
                "invocation_count": 1,
            },
        ],
        prompts=[
            {"name": "manager-system", "role": "manager", "sha256": _sha("prompt-manager")},
            {"name": "qa-system", "role": "qa", "sha256": _sha("prompt-qa")},
        ],
        adapters=[
            {
                "name": "claude",
                "version": "1.4.0",
                "sha256": _sha("adapter-claude"),
                "binary": "claude",
            },
        ],
        tools=[
            {"name": "git", "kind": "shell", "sha256": _sha("tool-git")},
        ],
        data_sources=[
            {"uri": "git+https://github.com/x/y@deadbeef", "kind": "repo", "sha256": _sha("source-x")},
        ],
    )


# ---------------------------------------------------------------------------
# 1. Dataclass invariants
# ---------------------------------------------------------------------------


class TestDataclassInvariants:
    def test_model_entry_rejects_bad_sha(self) -> None:
        with pytest.raises(BOMError):
            ModelEntry(name="m", provider="p", version="v", sha256="abc", invocation_count=1)

    def test_prompt_entry_rejects_bad_sha(self) -> None:
        with pytest.raises(BOMError):
            PromptEntry(name="n", role="r", sha256="not-a-sha")

    def test_adapter_entry_rejects_bad_sha(self) -> None:
        with pytest.raises(BOMError):
            AdapterEntry(name="n", version="v", sha256="x", binary="x")

    def test_tool_entry_rejects_bad_sha(self) -> None:
        with pytest.raises(BOMError):
            ToolEntry(name="n", kind="k", sha256="x")

    def test_data_source_entry_rejects_bad_sha(self) -> None:
        with pytest.raises(BOMError):
            DataSourceEntry(uri="u", kind="k", sha256="x")

    def test_model_entry_rejects_negative_invocations(self) -> None:
        with pytest.raises(BOMError):
            ModelEntry(
                name="m",
                provider="p",
                version="v",
                sha256=_sha("x"),
                invocation_count=-1,
            )

    def test_aibom_rejects_empty_run_id(self) -> None:
        with pytest.raises(BOMError):
            AIBOM(
                schema=BOM_SCHEMA_URL,
                schema_version=BOM_SCHEMA_VERSION,
                run_id="",
                started_at="x",
                finished_at="x",
                lineage_root_hash=_root_hash(),
                bernstein_version="0+u",
                models=(),
                prompts=(),
                adapters=(),
                tools=(),
                data_sources=(),
            )

    def test_aibom_rejects_bad_schema_version(self) -> None:
        with pytest.raises(BOMError):
            AIBOM(
                schema=BOM_SCHEMA_URL,
                schema_version="9.9",
                run_id="r1",
                started_at="x",
                finished_at="x",
                lineage_root_hash=_root_hash(),
                bernstein_version="0+u",
                models=(),
                prompts=(),
                adapters=(),
                tools=(),
                data_sources=(),
            )

    def test_aibom_rejects_bad_root_hash(self) -> None:
        with pytest.raises(BOMError):
            AIBOM(
                schema=BOM_SCHEMA_URL,
                schema_version=BOM_SCHEMA_VERSION,
                run_id="r1",
                started_at="x",
                finished_at="x",
                lineage_root_hash="abc",
                bernstein_version="0+u",
                models=(),
                prompts=(),
                adapters=(),
                tools=(),
                data_sources=(),
            )

    def test_model_entry_is_frozen(self) -> None:
        entry = ModelEntry(
            name="m",
            provider="p",
            version="v",
            sha256=_sha("m"),
            invocation_count=1,
        )
        # Frozen dataclass with slots raises FrozenInstanceError / AttributeError
        # depending on Python version; covering the union catches both.
        with pytest.raises((AttributeError, TypeError)):
            entry.name = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 2. generate_bom: snapshot validation
# ---------------------------------------------------------------------------


class TestGenerateBOMValidation:
    def test_missing_run_id_raises(self) -> None:
        snap = _minimal_snapshot()
        del snap["run_id"]
        with pytest.raises(BOMError, match="run_id"):
            generate_bom(snap)

    def test_missing_started_at_raises(self) -> None:
        snap = _minimal_snapshot()
        del snap["started_at"]
        with pytest.raises(BOMError, match="started_at"):
            generate_bom(snap)

    def test_missing_finished_at_raises(self) -> None:
        snap = _minimal_snapshot()
        del snap["finished_at"]
        with pytest.raises(BOMError, match="finished_at"):
            generate_bom(snap)

    def test_missing_lineage_root_hash_raises(self) -> None:
        snap = _minimal_snapshot()
        del snap["lineage_root_hash"]
        with pytest.raises(BOMError, match="lineage_root_hash"):
            generate_bom(snap)

    def test_empty_lists_produce_empty_tuples(self) -> None:
        bom = generate_bom(_minimal_snapshot())
        assert bom.models == ()
        assert bom.prompts == ()
        assert bom.adapters == ()
        assert bom.tools == ()
        assert bom.data_sources == ()

    def test_default_bernstein_version_when_absent(self) -> None:
        snap = _minimal_snapshot()
        snap.pop("bernstein_version")
        bom = generate_bom(snap)
        assert bom.bernstein_version == "0+unknown"

    def test_bad_sha_in_model_propagates(self) -> None:
        snap = _minimal_snapshot(
            models=[{"name": "n", "provider": "p", "version": "v", "sha256": "x"}],
        )
        with pytest.raises(BOMError):
            generate_bom(snap)


# ---------------------------------------------------------------------------
# 3. generate_bom: determinism, dedupe, sort
# ---------------------------------------------------------------------------


class TestGenerateBOMDeterminism:
    def test_full_snapshot_produces_expected_counts(self) -> None:
        bom = generate_bom(_full_snapshot())
        assert len(bom.models) == 2
        assert len(bom.prompts) == 2
        assert len(bom.adapters) == 1
        assert len(bom.tools) == 1
        assert len(bom.data_sources) == 1

    def test_duplicate_models_collapse_to_summed_invocations(self) -> None:
        sha = _sha("m1")
        snap = _minimal_snapshot(
            models=[
                {"name": "m", "provider": "p", "version": "v", "sha256": sha, "invocation_count": 2},
                {"name": "m", "provider": "p", "version": "v", "sha256": sha, "invocation_count": 5},
            ],
        )
        bom = generate_bom(snap)
        assert len(bom.models) == 1
        assert bom.models[0].invocation_count == 7

    def test_duplicate_prompts_collapse(self) -> None:
        sha = _sha("prompt-x")
        snap = _minimal_snapshot(
            prompts=[
                {"name": "x", "role": "r", "sha256": sha},
                {"name": "x", "role": "r", "sha256": sha},
            ],
        )
        bom = generate_bom(snap)
        assert len(bom.prompts) == 1

    def test_duplicate_adapters_collapse(self) -> None:
        sha = _sha("adapter-x")
        snap = _minimal_snapshot(
            adapters=[
                {"name": "x", "version": "1", "sha256": sha, "binary": "x"},
                {"name": "x", "version": "1", "sha256": sha, "binary": "x"},
            ],
        )
        bom = generate_bom(snap)
        assert len(bom.adapters) == 1

    def test_duplicate_tools_collapse(self) -> None:
        sha = _sha("tool-x")
        snap = _minimal_snapshot(
            tools=[
                {"name": "x", "kind": "k", "sha256": sha},
                {"name": "x", "kind": "k", "sha256": sha},
            ],
        )
        bom = generate_bom(snap)
        assert len(bom.tools) == 1

    def test_duplicate_data_sources_collapse(self) -> None:
        sha = _sha("source-x")
        snap = _minimal_snapshot(
            data_sources=[
                {"uri": "u", "kind": "k", "sha256": sha},
                {"uri": "u", "kind": "k", "sha256": sha},
            ],
        )
        bom = generate_bom(snap)
        assert len(bom.data_sources) == 1

    def test_models_sorted_by_provider_name_version(self) -> None:
        snap = _minimal_snapshot(
            models=[
                {"name": "z", "provider": "b", "version": "1", "sha256": _sha("z")},
                {"name": "a", "provider": "a", "version": "1", "sha256": _sha("a")},
            ],
        )
        bom = generate_bom(snap)
        assert bom.models[0].provider == "a"
        assert bom.models[1].provider == "b"

    def test_same_snapshot_yields_same_bom(self) -> None:
        bom1 = generate_bom(_full_snapshot())
        bom2 = generate_bom(_full_snapshot())
        assert bom1 == bom2

    def test_input_list_order_does_not_affect_output(self) -> None:
        snap_a = _full_snapshot()
        snap_b = _full_snapshot()
        snap_b["models"] = list(reversed(snap_b["models"]))
        snap_b["prompts"] = list(reversed(snap_b["prompts"]))
        assert generate_bom(snap_a) == generate_bom(snap_b)


# ---------------------------------------------------------------------------
# 3b. Dedupe identity coverage: every recorded field is either part of the
#     identity key or merged commutatively. A field that is neither makes
#     the survivor of a collision depend on input order.
# ---------------------------------------------------------------------------


# Fields the deduper deliberately folds together instead of distinguishing.
# Folding must be commutative (summation) so it stays order-independent.
_MERGED_FIELDS: dict[str, frozenset[str]] = {
    "models": frozenset({"invocation_count"}),
    "prompts": frozenset(),
    "adapters": frozenset(),
    "tools": frozenset(),
    "data_sources": frozenset(),
}

_BASE_ENTRY: dict[str, dict[str, Any]] = {
    "models": {
        "name": "m",
        "provider": "p",
        "version": "1",
        "sha256": _sha("model-base"),
        "invocation_count": 1,
    },
    "prompts": {"name": "p", "role": "r", "sha256": _sha("prompt-base")},
    "adapters": {"name": "a", "version": "1", "sha256": _sha("adapter-base"), "binary": "bin"},
    "tools": {"name": "t", "kind": "k", "sha256": _sha("tool-base")},
    "data_sources": {"uri": "u", "kind": "k", "sha256": _sha("source-base")},
}


def _variant(field: str, value: Any) -> Any:
    """Return a value for ``field`` distinct from ``value`` but still valid."""
    if field == "sha256":
        return _sha("variant")
    return str(value) + "-variant"


def _distinguishing_cases() -> list[tuple[str, str]]:
    return [
        (list_key, field)
        for list_key, base in _BASE_ENTRY.items()
        for field in base
        if field not in _MERGED_FIELDS[list_key]
    ]


class TestDedupeIdentityCoverage:
    """Guards the whole class of order-dependent-survivor bugs.

    A field that is recorded on an entry but absent from the dedupe key
    is silently dropped on collision, and *which* value survives depends
    on input order. Parametrising over every field of every entry type
    means a newly added field cannot reintroduce the defect unnoticed.
    """

    @pytest.mark.parametrize(("list_key", "field"), _distinguishing_cases())
    def test_entries_differing_in_one_field_stay_distinct(self, list_key: str, field: str) -> None:
        base = dict(_BASE_ENTRY[list_key])
        other = dict(base, **{field: _variant(field, base[field])})
        bom = generate_bom(_minimal_snapshot(**{list_key: [base, other]}))
        assert len(getattr(bom, list_key)) == 2, (
            f"{list_key} entries differing only in {field!r} were collapsed; {field!r} is missing from the identity key"
        )

    @pytest.mark.parametrize(("list_key", "field"), _distinguishing_cases())
    def test_permutation_invariant_for_one_field_difference(self, list_key: str, field: str) -> None:
        base = dict(_BASE_ENTRY[list_key])
        other = dict(base, **{field: _variant(field, base[field])})
        forward = generate_bom(_minimal_snapshot(**{list_key: [base, other]}))
        reverse = generate_bom(_minimal_snapshot(**{list_key: [other, base]}))
        assert forward == reverse

    def test_adapters_differing_only_in_binary_are_distinct(self) -> None:
        """Regression: the survivor used to depend on input order."""
        sha = _sha("adapter-shared")
        first = {"name": "a", "version": "1", "sha256": sha, "binary": "bin-a"}
        second = {"name": "a", "version": "1", "sha256": sha, "binary": "bin-b"}
        forward = generate_bom(_minimal_snapshot(adapters=[first, second]))
        reverse = generate_bom(_minimal_snapshot(adapters=[second, first]))
        assert forward == reverse
        assert [a.binary for a in forward.adapters] == ["bin-a", "bin-b"]

    def test_binary_participates_in_canonical_ordering(self) -> None:
        """Ordering must be total, so the tamper check can spot a swap."""
        sha = _sha("adapter-shared")
        entries = [{"name": "a", "version": "1", "sha256": sha, "binary": b} for b in ("b3", "b1", "b2")]
        bom = generate_bom(_minimal_snapshot(adapters=entries))
        assert [a.binary for a in bom.adapters] == ["b1", "b2", "b3"]
        doc = json.loads(encode_bom(bom, fmt="json"))
        doc["adapters"][0], doc["adapters"][1] = doc["adapters"][1], doc["adapters"][0]
        assert verify_bom(json.dumps(doc)).ok is False


# ---------------------------------------------------------------------------
# 4. Pure projection: no I/O during generate
# ---------------------------------------------------------------------------


class TestPureProjection:
    def test_no_writes_during_generate(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
        """generate_bom must not write any files. Critical invariant.

        Issue #1371 mandates that the BOM module is a pure projection
        over existing state. Re-recording its own facts would create a
        new source of truth.
        """
        original_open = open
        write_calls: list[str] = []

        def _spy_open(*args: Any, **kwargs: Any) -> Any:
            mode = kwargs.get("mode") if "mode" in kwargs else (args[1] if len(args) > 1 else "r")
            if "w" in mode or "a" in mode or "x" in mode:
                write_calls.append(str(args[0]))
            return original_open(*args, **kwargs)

        monkeypatch.setattr("builtins.open", _spy_open)
        generate_bom(_full_snapshot())
        assert write_calls == []

    def test_generate_does_not_mutate_snapshot(self) -> None:
        snap = _full_snapshot()
        before = json.dumps(snap, sort_keys=True)
        generate_bom(snap)
        after = json.dumps(snap, sort_keys=True)
        assert before == after


# ---------------------------------------------------------------------------
# 5. Encoders
# ---------------------------------------------------------------------------


class TestEncoders:
    def test_supported_formats_constant(self) -> None:
        assert {"json", "cyclonedx", "spdx"} == SUPPORTED_FORMATS

    def test_native_json_roundtrip_keys(self) -> None:
        bom = generate_bom(_full_snapshot())
        encoded = encode_bom(bom, fmt="json")
        decoded = json.loads(encoded)
        assert decoded["run_id"] == bom.run_id
        assert decoded["lineage_root_hash"] == bom.lineage_root_hash
        assert decoded["schema_version"] == BOM_SCHEMA_VERSION
        assert len(decoded["models"]) == 2

    def test_native_json_is_deterministic(self) -> None:
        bom = generate_bom(_full_snapshot())
        assert encode_bom(bom, fmt="json") == encode_bom(bom, fmt="json")

    def test_cyclonedx_keys_present(self) -> None:
        bom = generate_bom(_full_snapshot())
        decoded = json.loads(encode_bom(bom, fmt="cyclonedx"))
        assert decoded["bomFormat"] == "CycloneDX"
        assert decoded["specVersion"] == "1.5"
        assert "components" in decoded
        assert any(c["type"] == "machine-learning-model" for c in decoded["components"])

    def test_cyclonedx_carries_run_id_property(self) -> None:
        bom = generate_bom(_full_snapshot())
        decoded = json.loads(encode_bom(bom, fmt="cyclonedx"))
        props = {p["name"]: p["value"] for p in decoded["metadata"]["properties"]}
        assert props["bernstein:run_id"] == bom.run_id
        assert props["bernstein:lineage_root_hash"] == bom.lineage_root_hash

    def test_cyclonedx_hashes_are_sha256_blocks(self) -> None:
        bom = generate_bom(_full_snapshot())
        decoded = json.loads(encode_bom(bom, fmt="cyclonedx"))
        for component in decoded["components"]:
            for h in component["hashes"]:
                assert h["alg"] == "SHA-256"
                assert len(h["content"]) == 64

    def test_cyclonedx_serial_number_per_run(self) -> None:
        bom_a = generate_bom(_full_snapshot())
        snap_b = _full_snapshot()
        snap_b["run_id"] = "different"
        bom_b = generate_bom(snap_b)
        a = json.loads(encode_bom(bom_a, fmt="cyclonedx"))
        b = json.loads(encode_bom(bom_b, fmt="cyclonedx"))
        assert a["serialNumber"] != b["serialNumber"]

    def test_spdx_keys_present(self) -> None:
        bom = generate_bom(_full_snapshot())
        decoded = json.loads(encode_bom(bom, fmt="spdx"))
        assert decoded["spdxVersion"] == "SPDX-2.3"
        assert decoded["dataLicense"] == "CC0-1.0"
        assert decoded["SPDXID"] == "SPDXRef-DOCUMENT"
        assert decoded["name"].startswith("bernstein-ai-bom-")

    def test_spdx_packages_carry_sha256_checksums(self) -> None:
        bom = generate_bom(_full_snapshot())
        decoded = json.loads(encode_bom(bom, fmt="spdx"))
        for pkg in decoded["packages"]:
            assert pkg["checksums"][0]["algorithm"] == "SHA256"
            assert len(pkg["checksums"][0]["checksumValue"]) == 64

    def test_spdx_comment_carries_run_id_and_root(self) -> None:
        bom = generate_bom(_full_snapshot())
        decoded = json.loads(encode_bom(bom, fmt="spdx"))
        comment = decoded["creationInfo"]["comment"]
        assert bom.run_id in comment
        assert bom.lineage_root_hash in comment

    def test_encode_bom_unknown_format(self) -> None:
        bom = generate_bom(_minimal_snapshot())
        with pytest.raises(BOMError, match="unsupported"):
            encode_bom(bom, fmt="xml")


# ---------------------------------------------------------------------------
# 6. Snapshot test for CycloneDX-AI JSON shape
# ---------------------------------------------------------------------------


class TestCycloneDXSnapshot:
    """Pin the exact CycloneDX-AI shape for one canonical fixture.

    A change here forces the reviewer to consciously update the wire
    contract -- which is exactly what we want for a compliance surface.
    """

    def test_canonical_cyclonedx_shape(self) -> None:
        snap = {
            "run_id": "fixture-run-001",
            "started_at": "2026-01-01T00:00:00Z",
            "finished_at": "2026-01-01T00:01:00Z",
            "lineage_root_hash": "sha256:" + "a" * 64,
            "bernstein_version": "9.9.9",
            "models": [
                {
                    "name": "model-x",
                    "provider": "acme",
                    "version": "v1",
                    "sha256": "sha256:" + "b" * 64,
                    "invocation_count": 1,
                },
            ],
            "prompts": [
                {"name": "p1", "role": "manager", "sha256": "sha256:" + "c" * 64},
            ],
            "adapters": [
                {
                    "name": "a1",
                    "version": "1.0",
                    "sha256": "sha256:" + "d" * 64,
                    "binary": "a1",
                },
            ],
            "tools": [{"name": "t1", "kind": "shell", "sha256": "sha256:" + "e" * 64}],
            "data_sources": [{"uri": "u1", "kind": "repo", "sha256": "sha256:" + "f" * 64}],
        }
        bom = generate_bom(snap)
        decoded = json.loads(encode_bom(bom, fmt="cyclonedx"))

        # Top-level frame
        assert decoded["bomFormat"] == "CycloneDX"
        assert decoded["specVersion"] == "1.5"
        assert decoded["version"] == 1
        assert decoded["serialNumber"] == "urn:uuid:bernstein-ai-bom:fixture-run-001"

        # metadata.tools points at bernstein
        assert decoded["metadata"]["tools"][0]["name"] == "bernstein"
        assert decoded["metadata"]["tools"][0]["version"] == "9.9.9"

        # Components: 1 model + 1 prompt + 1 adapter + 1 tool + 1 source = 5
        assert len(decoded["components"]) == 5
        kinds = [c["type"] for c in decoded["components"]]
        assert "machine-learning-model" in kinds
        # The model carries provider as `publisher`
        model_comp = next(c for c in decoded["components"] if c["type"] == "machine-learning-model")
        assert model_comp["publisher"] == "acme"
        assert model_comp["version"] == "v1"


# ---------------------------------------------------------------------------
# 7. verify_bom
# ---------------------------------------------------------------------------


class TestVerifyBOM:
    def test_passes_for_freshly_encoded_bom(self) -> None:
        bom = generate_bom(_full_snapshot())
        report = verify_bom(encode_bom(bom, fmt="json"))
        assert report.ok is True
        assert report.errors == ()
        assert report.checked_count >= 5

    def test_rejects_garbage_payload(self) -> None:
        report = verify_bom(b"\xff\xfe not json")
        assert report.ok is False
        assert any("JSON" in e or "UTF-8" in e for e in report.errors)

    def test_rejects_non_object_payload(self) -> None:
        report = verify_bom(b"[1,2,3]")
        assert report.ok is False

    def test_rejects_wrong_schema_version(self) -> None:
        doc = json.loads(encode_bom(generate_bom(_full_snapshot()), fmt="json"))
        doc["schema_version"] = "0.1"
        report = verify_bom(json.dumps(doc).encode("utf-8"))
        assert report.ok is False
        assert any("schema_version" in e for e in report.errors)

    def test_rejects_missing_run_id(self) -> None:
        doc = json.loads(encode_bom(generate_bom(_full_snapshot()), fmt="json"))
        doc["run_id"] = ""
        report = verify_bom(json.dumps(doc).encode("utf-8"))
        assert report.ok is False

    def test_rejects_bad_root_hash(self) -> None:
        doc = json.loads(encode_bom(generate_bom(_full_snapshot()), fmt="json"))
        doc["lineage_root_hash"] = "not-a-sha"
        report = verify_bom(json.dumps(doc).encode("utf-8"))
        assert report.ok is False

    def test_detects_bad_element_sha(self) -> None:
        doc = json.loads(encode_bom(generate_bom(_full_snapshot()), fmt="json"))
        doc["models"][0]["sha256"] = "garbage"
        report = verify_bom(json.dumps(doc).encode("utf-8"))
        assert report.ok is False
        assert any("sha256" in e for e in report.errors)

    def test_detects_reordering_attack(self) -> None:
        snap = _full_snapshot()
        snap["models"] = [
            {
                "name": "a",
                "provider": "a",
                "version": "1",
                "sha256": _sha("a"),
                "invocation_count": 1,
            },
            {
                "name": "b",
                "provider": "b",
                "version": "1",
                "sha256": _sha("b"),
                "invocation_count": 1,
            },
        ]
        doc = json.loads(encode_bom(generate_bom(snap), fmt="json"))
        doc["models"].reverse()  # Break sort order
        report = verify_bom(json.dumps(doc).encode("utf-8"))
        assert report.ok is False
        assert any("ordering" in e for e in report.errors)

    def test_accepts_mapping_payload(self) -> None:
        bom = generate_bom(_full_snapshot())
        doc = json.loads(encode_bom(bom, fmt="json"))
        report = verify_bom(doc)
        assert report.ok is True

    def test_accepts_str_payload(self) -> None:
        bom = generate_bom(_full_snapshot())
        report = verify_bom(encode_bom(bom, fmt="json").decode("utf-8"))
        assert report.ok is True

    def test_rejects_unsupported_payload_type(self) -> None:
        report = verify_bom(12345)  # type: ignore[arg-type]
        assert report.ok is False

    def test_reports_multiple_errors(self) -> None:
        bad: dict[str, Any] = {
            "schema_version": "0.1",
            "run_id": "",
            "lineage_root_hash": "nope",
            "models": [{"name": "x", "provider": "p", "version": "v", "sha256": "bad"}],
            "prompts": [],
            "adapters": [],
            "tools": [],
            "data_sources": [],
        }
        report = verify_bom(json.dumps(bad).encode("utf-8"))
        assert report.ok is False
        assert len(report.errors) >= 3


# ---------------------------------------------------------------------------
# 8. bom_content_hash
# ---------------------------------------------------------------------------


class TestBOMContentHash:
    def test_returns_sha256_prefix(self) -> None:
        bom = generate_bom(_full_snapshot())
        h = bom_content_hash(bom)
        assert h.startswith("sha256:")
        assert len(h) == len("sha256:") + 64

    def test_stable_across_calls(self) -> None:
        bom = generate_bom(_full_snapshot())
        assert bom_content_hash(bom) == bom_content_hash(bom)

    def test_changes_when_data_changes(self) -> None:
        bom_a = generate_bom(_full_snapshot())
        snap_b = _full_snapshot()
        snap_b["run_id"] = "different"
        bom_b = generate_bom(snap_b)
        assert bom_content_hash(bom_a) != bom_content_hash(bom_b)

    def test_independent_of_encoder(self) -> None:
        """Hash is over native JSON; format choice is irrelevant.

        This pins the "stable record id" property: a release tagged
        ``2.1.0`` whose BOM was emitted as CycloneDX and re-emitted as
        SPDX still surfaces the same logical record in the decision log.
        """
        bom = generate_bom(_full_snapshot())
        h1 = bom_content_hash(bom)
        # Encode in another format -- hash unchanged because we hash the
        # canonical native JSON, not the encoded bytes.
        encode_bom(bom, fmt="cyclonedx")
        encode_bom(bom, fmt="spdx")
        assert bom_content_hash(bom) == h1


# ---------------------------------------------------------------------------
# 9. Property-based tests with Hypothesis (≥15)
# ---------------------------------------------------------------------------


_hex_64 = st.text(alphabet="0123456789abcdef", min_size=64, max_size=64)


@st.composite
def _sha_strategy(draw: st.DrawFn) -> str:
    return "sha256:" + draw(_hex_64)


_safe_text = st.text(
    alphabet=st.characters(blacklist_categories=("Cc", "Cs"), max_codepoint=0x7F),
    min_size=1,
    max_size=12,
)


@st.composite
def _model_dict(draw: st.DrawFn) -> dict[str, Any]:
    return {
        "name": draw(_safe_text),
        "provider": draw(_safe_text),
        "version": draw(_safe_text),
        "sha256": draw(_sha_strategy()),
        "invocation_count": draw(st.integers(min_value=0, max_value=10_000)),
    }


@st.composite
def _prompt_dict(draw: st.DrawFn) -> dict[str, Any]:
    return {
        "name": draw(_safe_text),
        "role": draw(_safe_text),
        "sha256": draw(_sha_strategy()),
    }


@st.composite
def _adapter_dict(draw: st.DrawFn) -> dict[str, Any]:
    return {
        "name": draw(_safe_text),
        "version": draw(_safe_text),
        "sha256": draw(_sha_strategy()),
        "binary": draw(_safe_text),
    }


@st.composite
def _tool_dict(draw: st.DrawFn) -> dict[str, Any]:
    return {
        "name": draw(_safe_text),
        "kind": draw(_safe_text),
        "sha256": draw(_sha_strategy()),
    }


@st.composite
def _source_dict(draw: st.DrawFn) -> dict[str, Any]:
    return {
        "uri": draw(_safe_text),
        "kind": draw(_safe_text),
        "sha256": draw(_sha_strategy()),
    }


@st.composite
def _snapshot(draw: st.DrawFn) -> dict[str, Any]:
    return {
        "run_id": draw(_safe_text),
        "started_at": "2026-01-01T00:00:00Z",
        "finished_at": "2026-01-01T01:00:00Z",
        "lineage_root_hash": draw(_sha_strategy()),
        "bernstein_version": draw(_safe_text),
        "models": draw(st.lists(_model_dict(), max_size=5)),
        "prompts": draw(st.lists(_prompt_dict(), max_size=5)),
        "adapters": draw(st.lists(_adapter_dict(), max_size=5)),
        "tools": draw(st.lists(_tool_dict(), max_size=5)),
        "data_sources": draw(st.lists(_source_dict(), max_size=5)),
    }


_HYPOTHESIS_SETTINGS = settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


class TestProperties:
    @_HYPOTHESIS_SETTINGS
    @given(snap=_snapshot())
    def test_generate_is_idempotent(self, snap: dict[str, Any]) -> None:
        assume(snap["run_id"])
        bom1 = generate_bom(snap)
        bom2 = generate_bom(snap)
        assert bom1 == bom2

    @_HYPOTHESIS_SETTINGS
    @given(snap=_snapshot())
    def test_native_json_is_deterministic(self, snap: dict[str, Any]) -> None:
        assume(snap["run_id"])
        bom = generate_bom(snap)
        assert encode_bom(bom, fmt="json") == encode_bom(bom, fmt="json")

    @_HYPOTHESIS_SETTINGS
    @given(snap=_snapshot())
    def test_cyclonedx_is_deterministic(self, snap: dict[str, Any]) -> None:
        assume(snap["run_id"])
        bom = generate_bom(snap)
        assert encode_bom(bom, fmt="cyclonedx") == encode_bom(bom, fmt="cyclonedx")

    @_HYPOTHESIS_SETTINGS
    @given(snap=_snapshot())
    def test_spdx_is_deterministic(self, snap: dict[str, Any]) -> None:
        assume(snap["run_id"])
        bom = generate_bom(snap)
        assert encode_bom(bom, fmt="spdx") == encode_bom(bom, fmt="spdx")

    @_HYPOTHESIS_SETTINGS
    @given(snap=_snapshot())
    def test_input_permutation_invariant(self, snap: dict[str, Any]) -> None:
        assume(snap["run_id"])
        rev = snap.copy()
        rev["models"] = list(reversed(snap["models"]))
        rev["prompts"] = list(reversed(snap["prompts"]))
        rev["adapters"] = list(reversed(snap["adapters"]))
        rev["tools"] = list(reversed(snap["tools"]))
        rev["data_sources"] = list(reversed(snap["data_sources"]))
        assert generate_bom(snap) == generate_bom(rev)

    @_HYPOTHESIS_SETTINGS
    @given(snap=_snapshot())
    def test_freshly_emitted_bom_verifies(self, snap: dict[str, Any]) -> None:
        assume(snap["run_id"])
        bom = generate_bom(snap)
        report = verify_bom(encode_bom(bom, fmt="json"))
        assert report.ok is True, report.errors

    @_HYPOTHESIS_SETTINGS
    @given(snap=_snapshot())
    def test_content_hash_changes_with_run_id(self, snap: dict[str, Any]) -> None:
        assume(snap["run_id"])
        bom_a = generate_bom(snap)
        snap_b = snap.copy()
        snap_b["run_id"] = snap["run_id"] + "x"
        bom_b = generate_bom(snap_b)
        assert bom_content_hash(bom_a) != bom_content_hash(bom_b)

    @_HYPOTHESIS_SETTINGS
    @given(snap=_snapshot())
    def test_content_hash_stable_across_format(self, snap: dict[str, Any]) -> None:
        assume(snap["run_id"])
        bom = generate_bom(snap)
        h_before = bom_content_hash(bom)
        encode_bom(bom, fmt="cyclonedx")
        encode_bom(bom, fmt="spdx")
        h_after = bom_content_hash(bom)
        assert h_before == h_after

    @_HYPOTHESIS_SETTINGS
    @given(snap=_snapshot())
    def test_models_dedup_invariant_on_doubling(self, snap: dict[str, Any]) -> None:
        assume(snap["run_id"])
        doubled = snap.copy()
        doubled["models"] = snap["models"] + snap["models"]
        bom_single = generate_bom(snap)
        bom_doubled = generate_bom(doubled)
        # Count of unique models is unchanged; invocation counts double.
        assert len(bom_doubled.models) == len(bom_single.models)
        for original, dbl in zip(
            sorted(bom_single.models, key=lambda m: m.sha256),
            sorted(bom_doubled.models, key=lambda m: m.sha256),
            strict=False,
        ):
            assert dbl.invocation_count == 2 * original.invocation_count

    @_HYPOTHESIS_SETTINGS
    @given(snap=_snapshot())
    def test_models_sorted_after_dedup(self, snap: dict[str, Any]) -> None:
        assume(snap["run_id"])
        bom = generate_bom(snap)
        keys = [(m.provider, m.name, m.version, m.sha256) for m in bom.models]
        assert keys == sorted(keys)

    @_HYPOTHESIS_SETTINGS
    @given(snap=_snapshot())
    def test_prompts_sorted_after_dedup(self, snap: dict[str, Any]) -> None:
        assume(snap["run_id"])
        bom = generate_bom(snap)
        keys = [(p.role, p.name, p.sha256) for p in bom.prompts]
        assert keys == sorted(keys)

    @_HYPOTHESIS_SETTINGS
    @given(snap=_snapshot())
    def test_adapters_sorted_after_dedup(self, snap: dict[str, Any]) -> None:
        assume(snap["run_id"])
        bom = generate_bom(snap)
        keys = [(a.name, a.version, a.sha256) for a in bom.adapters]
        assert keys == sorted(keys)

    @_HYPOTHESIS_SETTINGS
    @given(snap=_snapshot())
    def test_tools_sorted_after_dedup(self, snap: dict[str, Any]) -> None:
        assume(snap["run_id"])
        bom = generate_bom(snap)
        keys = [(t.kind, t.name, t.sha256) for t in bom.tools]
        assert keys == sorted(keys)

    @_HYPOTHESIS_SETTINGS
    @given(snap=_snapshot())
    def test_data_sources_sorted_after_dedup(self, snap: dict[str, Any]) -> None:
        assume(snap["run_id"])
        bom = generate_bom(snap)
        keys = [(d.kind, d.uri, d.sha256) for d in bom.data_sources]
        assert keys == sorted(keys)

    @_HYPOTHESIS_SETTINGS
    @given(snap=_snapshot())
    def test_cyclonedx_components_sha256_well_formed(self, snap: dict[str, Any]) -> None:
        assume(snap["run_id"])
        bom = generate_bom(snap)
        decoded = json.loads(encode_bom(bom, fmt="cyclonedx"))
        for comp in decoded["components"]:
            for h in comp["hashes"]:
                assert h["alg"] == "SHA-256"
                assert len(h["content"]) == 64

    @_HYPOTHESIS_SETTINGS
    @given(snap=_snapshot())
    def test_spdx_packages_sha256_well_formed(self, snap: dict[str, Any]) -> None:
        assume(snap["run_id"])
        bom = generate_bom(snap)
        decoded = json.loads(encode_bom(bom, fmt="spdx"))
        for pkg in decoded["packages"]:
            assert pkg["checksums"][0]["algorithm"] == "SHA256"
            assert len(pkg["checksums"][0]["checksumValue"]) == 64
