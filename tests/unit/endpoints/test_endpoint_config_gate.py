"""Config-surface tests for local endpoint profiles (issue #2356).

AC coverage:

* A ``local_endpoints`` profile validates and resolves role presets onto
  ``role_model_policy`` entries.
* AC3 -- an uncertified endpoint assigned to a gated role fails config
  validation with a clear message.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.config.config_schema import BernsteinConfig, load_and_validate
from bernstein.core.endpoints.certification import (
    build_endpoint_certification,
    load_or_create_endpoint_identity,
)
from bernstein.core.endpoints.conformance import evaluate_roles, run_conformance
from tests.unit.endpoints.stub_endpoint import FakeTransport

_BASE_URL = "http://127.0.0.1:11434/v1"
_MODEL = "tiny-coder"
_KEY = b"0" * 32


def _config_data(role: str) -> dict[str, object]:
    return {
        "goal": "demo",
        "local_endpoints": {
            "workhorse": {
                "base_url": _BASE_URL,
                "model": _MODEL,
                "engine": "stub",
            }
        },
        "role_model_policy": {role: {"endpoint": "workhorse"}},
    }


def _write_config(tmp_path: Path, role: str) -> Path:
    import yaml

    path = tmp_path / "bernstein.yaml"
    path.write_text(yaml.safe_dump(_config_data(role)), encoding="utf-8")
    return path


def _certify(tmp_path: Path, roles: tuple[str, ...]) -> None:
    transcript = run_conformance(base_url=_BASE_URL, model=_MODEL, transport=FakeTransport())
    priv, pub = load_or_create_endpoint_identity(tmp_path / ".sdd" / "identity")
    build_endpoint_certification(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        private_key_pem=priv,
        public_key_pem=pub,
        transcript=transcript,
        verdicts=evaluate_roles(transcript, roles),
        engine="stub",
        timestamp=1000,
    )


# ---------------------------------------------------------------------------
# Profile schema + role resolution
# ---------------------------------------------------------------------------


def test_local_endpoint_profile_resolves_onto_role_entry() -> None:
    config = BernsteinConfig.model_validate(_config_data("linter"))
    assert config.local_endpoints is not None
    entry = (config.role_model_policy or {})["linter"]
    assert entry.endpoint == "workhorse"
    assert entry.base_url == _BASE_URL
    assert entry.model == _MODEL


def test_unknown_endpoint_profile_name_is_rejected() -> None:
    data = _config_data("linter")
    data["role_model_policy"] = {"linter": {"endpoint": "missing-profile"}}
    with pytest.raises(ValueError, match="missing-profile"):
        BernsteinConfig.model_validate(data)


def test_endpoint_reference_conflicts_with_inline_base_url() -> None:
    data = _config_data("linter")
    data["role_model_policy"] = {"linter": {"endpoint": "workhorse", "base_url": "http://other/v1"}}
    with pytest.raises(ValueError, match="base_url"):
        BernsteinConfig.model_validate(data)


def test_profile_api_key_env_flows_to_role_entry() -> None:
    data = _config_data("linter")
    profiles = data["local_endpoints"]
    assert isinstance(profiles, dict)
    profiles["workhorse"]["api_key_env"] = "LOCAL_LLM_API_KEY"
    config = BernsteinConfig.model_validate(data)
    entry = (config.role_model_policy or {})["linter"]
    assert entry.api_key_env == "LOCAL_LLM_API_KEY"


# ---------------------------------------------------------------------------
# AC3: certification gate at config validation
# ---------------------------------------------------------------------------


def test_uncertified_endpoint_on_gated_role_fails_validation(tmp_path: Path) -> None:
    path = _write_config(tmp_path, "manager")
    with pytest.raises(ValueError, match="manager") as excinfo:
        load_and_validate(path)
    message = str(excinfo.value)
    assert "workhorse" in message
    assert "doctor --endpoint" in message


def test_certified_endpoint_on_gated_role_passes_validation(tmp_path: Path) -> None:
    path = _write_config(tmp_path, "manager")
    _certify(tmp_path, ("manager",))
    config = load_and_validate(path)
    entry = (config.role_model_policy or {})["manager"]
    assert entry.base_url == _BASE_URL


def test_uncertified_endpoint_on_low_stakes_role_passes_validation(tmp_path: Path) -> None:
    path = _write_config(tmp_path, "linter")
    config = load_and_validate(path)
    entry = (config.role_model_policy or {})["linter"]
    assert entry.base_url == _BASE_URL


def test_receipt_missing_the_gated_role_still_fails_validation(tmp_path: Path) -> None:
    path = _write_config(tmp_path, "manager")
    _certify(tmp_path, ("linter",))
    with pytest.raises(ValueError, match="manager"):
        load_and_validate(path)


def test_config_without_local_endpoints_is_unchanged(tmp_path: Path) -> None:
    import yaml

    path = tmp_path / "bernstein.yaml"
    path.write_text(
        yaml.safe_dump({"goal": "demo", "role_model_policy": {"backend": {"model": "gpt-5"}}}),
        encoding="utf-8",
    )
    config = load_and_validate(path)
    entry = (config.role_model_policy or {})["backend"]
    assert entry.endpoint is None
    assert entry.model == "gpt-5"
