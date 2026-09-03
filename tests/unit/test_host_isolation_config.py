"""The host-isolation declaration is layered config, not a bespoke file (#5341).

An operator running the CLI inside a container or VM they control states that
fact once, in the same precedence chain every other setting uses, and adapters
that own a vendor sandbox read it from there. These tests pin the vocabulary to
:class:`SandboxTier` so a tier can never be added in one place only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bernstein.adapters.capability_profile import SandboxTier
from bernstein.core.config.home import _DEFAULTS, _ENV_OVERRIDE_MAP, BernsteinHome  # type: ignore[reportPrivateUsage]
from bernstein.core.config.host_isolation import (
    HOST_ISOLATION_EVIDENCE_KEY,
    HOST_ISOLATION_TIER_KEY,
    allowed_tier_values,
    resolve_host_isolation,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _no_ambient_declaration(monkeypatch: pytest.MonkeyPatch) -> None:
    """A declaration inherited from the developer's shell would fake a pass."""
    monkeypatch.delenv("BERNSTEIN_HOST_ISOLATION_TIER", raising=False)
    monkeypatch.delenv("BERNSTEIN_HOST_ISOLATION_EVIDENCE", raising=False)


def _home(tmp_path: Path) -> BernsteinHome:
    return BernsteinHome(tmp_path / ".bernstein")


def _write_project_config(project_dir: Path, body: str) -> None:
    sdd_config = project_dir / ".sdd" / "config.yaml"
    sdd_config.parent.mkdir(parents=True, exist_ok=True)
    sdd_config.write_text(body)


class TestVocabulary:
    """The allowed values are the sandbox tiers, derived rather than restated."""

    def test_allowed_values_equal_the_sandbox_tiers(self) -> None:
        assert set(allowed_tier_values()) == {tier.value for tier in SandboxTier}

    def test_keys_are_registered_as_known_config(self) -> None:
        """`config get`/`set`/`list` operate on `_DEFAULTS`, so absence is invisibility."""
        assert _DEFAULTS[HOST_ISOLATION_TIER_KEY] == SandboxTier.NONE.value
        assert _DEFAULTS[HOST_ISOLATION_EVIDENCE_KEY] == ""

    def test_keys_have_environment_overrides(self) -> None:
        assert _ENV_OVERRIDE_MAP[HOST_ISOLATION_TIER_KEY] == "BERNSTEIN_HOST_ISOLATION_TIER"
        assert _ENV_OVERRIDE_MAP[HOST_ISOLATION_EVIDENCE_KEY] == "BERNSTEIN_HOST_ISOLATION_EVIDENCE"


class TestPrecedence:
    """Resolution rides the existing chain: env > project > user > default."""

    def test_default_is_no_isolation_and_no_evidence(self, tmp_path: Path) -> None:
        decl = resolve_host_isolation(tmp_path, home=_home(tmp_path))

        assert decl.tier is SandboxTier.NONE
        assert decl.evidence == ""
        assert decl.source == "default"

    def test_user_config_declares_the_tier(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        home.set(HOST_ISOLATION_TIER_KEY, "container")
        home.set(HOST_ISOLATION_EVIDENCE_KEY, "read-only rootfs, cap-drop ALL")

        decl = resolve_host_isolation(tmp_path, home=home)

        assert decl.tier is SandboxTier.CONTAINER
        assert decl.evidence == "read-only rootfs, cap-drop ALL"
        assert decl.source == "global"

    def test_project_config_overrides_user_config(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        home.set(HOST_ISOLATION_TIER_KEY, "container")
        _write_project_config(tmp_path, "host_isolation_tier: vm\nhost_isolation_evidence: firecracker microVM\n")

        decl = resolve_host_isolation(tmp_path, home=home)

        assert decl.tier is SandboxTier.VM
        assert decl.evidence == "firecracker microVM"
        assert decl.source == "project"

    def test_environment_overrides_project_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home = _home(tmp_path)
        home.set(HOST_ISOLATION_TIER_KEY, "container")
        _write_project_config(tmp_path, "host_isolation_tier: vm\n")
        monkeypatch.setenv("BERNSTEIN_HOST_ISOLATION_TIER", "process")
        monkeypatch.setenv("BERNSTEIN_HOST_ISOLATION_EVIDENCE", "seccomp profile")

        decl = resolve_host_isolation(tmp_path, home=home)

        assert decl.tier is SandboxTier.PROCESS
        assert decl.evidence == "seccomp profile"
        assert decl.source == "session"

    def test_explicit_none_stays_the_weakest_tier(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """``none`` is a real tier name, not a request to unset the key."""
        monkeypatch.setenv("BERNSTEIN_HOST_ISOLATION_TIER", "none")

        decl = resolve_host_isolation(tmp_path, home=_home(tmp_path))

        assert decl.tier is SandboxTier.NONE

    def test_tier_is_case_and_whitespace_insensitive(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BERNSTEIN_HOST_ISOLATION_TIER", "  Container ")

        assert resolve_host_isolation(tmp_path, home=_home(tmp_path)).tier is SandboxTier.CONTAINER


class TestInvalidTier:
    """A misdeclared tier fails loudly; guessing would drop a vendor sandbox."""

    def test_unknown_tier_raises_naming_every_allowed_value(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        home.set(HOST_ISOLATION_TIER_KEY, "kubernetes")

        with pytest.raises(ValueError) as excinfo:
            resolve_host_isolation(tmp_path, home=home)

        message = str(excinfo.value)
        assert "kubernetes" in message
        assert HOST_ISOLATION_TIER_KEY in message
        for value in allowed_tier_values():
            assert value in message

    def test_non_string_tier_raises(self, tmp_path: Path) -> None:
        _write_project_config(tmp_path, "host_isolation_tier: true\n")

        with pytest.raises(ValueError, match=HOST_ISOLATION_TIER_KEY):
            resolve_host_isolation(tmp_path, home=_home(tmp_path))
