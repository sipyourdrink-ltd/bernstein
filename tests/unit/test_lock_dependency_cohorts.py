"""Locked OTel SDK releases retain their exact API dependency."""

import tomllib
from pathlib import Path


def test_locked_otel_sdk_and_api_satisfy_the_exact_release_dependency() -> None:
    """The SDK METADATA pins API to its own release; audit alone misses mismatch."""
    lock = tomllib.loads((Path(__file__).resolve().parents[2] / "uv.lock").read_text(encoding="utf-8"))
    packages = {package["name"]: package for package in lock["package"]}
    assert packages["opentelemetry-sdk"]["version"] == packages["opentelemetry-api"]["version"], (
        "opentelemetry-sdk requires the same exact opentelemetry-api release"
    )
