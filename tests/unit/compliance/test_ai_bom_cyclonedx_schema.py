"""The emitted CycloneDX document validates against the official 1.7 schema.

The schema is vendored under ``tests/fixtures/cyclonedx/`` (see its README for
the upstream provenance), so these checks run offline. Two properties are
pinned here:

1. The emitted document is *structurally legal* CycloneDX -- the contract
   procurement and SBOM consumers parse. The encoder previously emitted a
   ``serialNumber`` that the schema rejects, which nothing caught because no
   test ever validated a document; the regression is pinned below.
2. The specification pin in the encoder and the vendored schema name the same
   version, so bumping one without the other fails here rather than shipping a
   document that claims a specification its bytes were never checked against.
"""

from __future__ import annotations

import json
import re
from typing import Any

from bernstein.core.compliance.ai_bom import encode_bom, generate_bom
from bernstein.core.compliance.ai_bom_encoders.cyclonedx import (
    _SERIAL_NAMESPACE,
    CYCLONEDX_SCHEMA_URL,
    CYCLONEDX_SPEC_VERSION,
)
from tests.fixtures.cyclonedx import BOM_SCHEMA_NAME, FIXTURE_DIR, validate_cyclonedx

_UUID_URN_RE = re.compile(r"^urn:uuid:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

_MODEL_SHA = "sha256:" + "b" * 64


def _snapshot(**overrides: Any) -> dict[str, Any]:
    snap: dict[str, Any] = {
        "run_id": "20260927-160000",
        "started_at": "2026-09-27T16:00:00Z",
        "finished_at": "2026-09-27T16:01:00Z",
        "lineage_root_hash": "sha256:" + "a" * 64,
        "bernstein_version": "9.9.9",
        "models": [
            {
                "name": "claude-3-7-sonnet",
                "provider": "anthropic",
                "version": "2026-02-15",
                "sha256": _MODEL_SHA,
                "invocation_count": 3,
            },
        ],
        "prompts": [{"name": "manager-system", "role": "manager", "sha256": "sha256:" + "c" * 64}],
        "adapters": [
            {"name": "claude-code", "version": "1.2.3", "sha256": "sha256:" + "d" * 64, "binary": "/usr/bin/claude"},
        ],
        "tools": [{"name": "filesystem", "kind": "mcp", "sha256": "sha256:" + "e" * 64}],
        "data_sources": [{"uri": "https://example.test/data.json", "kind": "http", "sha256": "sha256:" + "f" * 64}],
    }
    snap.update(overrides)
    return snap


def _document(**overrides: Any) -> dict[str, Any]:
    decoded: dict[str, Any] = json.loads(encode_bom(generate_bom(_snapshot(**overrides)), fmt="cyclonedx"))
    return decoded


def _schema() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((FIXTURE_DIR / BOM_SCHEMA_NAME).read_text(encoding="utf-8"))
    return loaded


class TestDocumentValidates:
    """The document a run emits is legal CycloneDX 1.7."""

    def test_full_document_validates(self) -> None:
        assert validate_cyclonedx(_document()) == []

    def test_document_without_components_validates(self) -> None:
        assert validate_cyclonedx(_document(models=[], prompts=[], adapters=[], tools=[], data_sources=[])) == []

    def test_lineage_shaped_model_validates(self) -> None:
        """A lineage-derived BOM carries empty provider/version and no endpoints."""
        document = _document(
            models=[
                {
                    "name": "claude-3-7-sonnet",
                    "provider": "",
                    "version": "",
                    "sha256": _MODEL_SHA,
                    "invocation_count": 1,
                },
            ],
        )
        assert validate_cyclonedx(document) == []

    def test_model_without_identifier_still_validates(self) -> None:
        document = _document(
            models=[{"name": "", "provider": "", "version": "", "sha256": _MODEL_SHA, "invocation_count": 1}],
        )
        assert validate_cyclonedx(document) == []

    def test_the_old_serial_number_shape_is_rejected(self) -> None:
        """Regression: the previous serial number was not a UUID URN.

        Without this, a validator that never fails would make every check above
        vacuous.
        """
        document = _document()
        document["serialNumber"] = "urn:uuid:bernstein-ai-bom:20260927-160000"
        errors = validate_cyclonedx(document)
        assert errors and all("serialNumber" in error for error in errors)


class TestSpecificationPin:
    """Encoder pin and vendored schema agree on the version."""

    def test_pinned_version_is_1_7(self) -> None:
        assert CYCLONEDX_SPEC_VERSION == "1.7"
        assert CYCLONEDX_SCHEMA_URL == "http://cyclonedx.org/schema/bom-1.7.schema.json"

    def test_emitted_schema_url_matches_the_vendored_schema_id(self) -> None:
        assert _schema()["$id"] == _document()["$schema"] == CYCLONEDX_SCHEMA_URL

    def test_vendored_schema_is_the_one_the_spec_version_names(self) -> None:
        assert f"bom-{CYCLONEDX_SPEC_VERSION}.schema.json" == BOM_SCHEMA_NAME

    def test_the_three_emitters_pin_the_same_version(self) -> None:
        """One release must not ship two CycloneDX versions.

        Each emitter's own test pins its own version, so a drift fails
        *somewhere*; this asserts the three agree with each other, which is the
        property the lockstep claim actually makes.
        """
        from bernstein.core.security import compliance as security_compliance
        from bernstein.core.security import sbom as security_sbom

        pins = {
            CYCLONEDX_SPEC_VERSION,
            security_sbom._CYCLONEDX_SPEC_VERSION,
            security_compliance._CYCLONEDX_SPEC_VERSION,
        }
        assert len(pins) == 1, f"emitters disagree on the specification version: {sorted(pins)}"

    def test_documents_do_not_share_a_serial_namespace(self) -> None:
        """Distinct documents deserve distinct serial spaces."""
        from bernstein.core.security.sbom import _SERIAL_NAMESPACE as dependency_sbom_namespace

        assert dependency_sbom_namespace != _SERIAL_NAMESPACE


class TestModelCard:
    """Only facts the run recorded reach the ML-BOM model card."""

    def test_identifier_is_projected_as_model_architecture(self) -> None:
        model = next(c for c in _document()["components"] if c["type"] == "machine-learning-model")
        assert model["modelCard"] == {"modelParameters": {"modelArchitecture": "claude-3-7-sonnet"}}

    def test_model_card_omitted_when_no_identifier_was_recorded(self) -> None:
        document = _document(
            models=[{"name": "", "provider": "", "version": "", "sha256": _MODEL_SHA, "invocation_count": 1}],
        )
        model = next(c for c in document["components"] if c["type"] == "machine-learning-model")
        assert "modelCard" not in model

    def test_unrecorded_ml_bom_fields_are_absent(self) -> None:
        """task / architectureFamily / datasets / performanceMetrics are not recorded."""
        model = next(c for c in _document()["components"] if c["type"] == "machine-learning-model")
        parameters = model["modelCard"]["modelParameters"]
        for field in ("task", "architectureFamily", "approach", "datasets", "inputs", "outputs"):
            assert field not in parameters
        assert "quantitativeAnalysis" not in model["modelCard"]


class TestSerialNumber:
    """The serial number is a schema-legal, per-run, deterministic UUID URN."""

    def test_is_a_uuid_urn(self) -> None:
        assert _UUID_URN_RE.match(_document()["serialNumber"])

    def test_is_stable_across_encodings(self) -> None:
        first = encode_bom(generate_bom(_snapshot()), fmt="cyclonedx")
        second = encode_bom(generate_bom(_snapshot()), fmt="cyclonedx")
        assert first == second

    def test_differs_per_run(self) -> None:
        other = _document(run_id="20260927-170000")
        assert other["serialNumber"] != _document()["serialNumber"]
