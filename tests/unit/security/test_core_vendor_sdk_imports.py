"""``bernstein.core`` may not gain a vendor SDK import (issue #4984).

A secret store, an object store, or a queue that needs a vendor SDK belongs
behind a contract that core calls, in an adapter or plugin that core does not
import. The sites below predate the external-store contract and are the only
ones allowed; the set is a ratchet that only ever shrinks as each site moves
out of core. Adding a site fails this test, and so does removing one without
shrinking the baseline, so the list cannot quietly drift out of date.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

CORE = Path(__file__).resolve().parents[3] / "src" / "bernstein" / "core"

#: Distribution roots that ship a vendor SDK. Matching is on the top-level
#: module name, so ``google.cloud`` and ``google.api_core`` both match.
VENDOR_SDK_ROOTS: frozenset[str] = frozenset(
    {
        "akeyless",
        "azure",
        "boto3",
        "botocore",
        "conjur",
        "cyberark",
        "doppler",
        "google",
        "googleapiclient",
        "hvac",
        "infisical",
        "keyring",
        "onepassword",
        "openbao",
    }
)

#: Directories holding generated code, which is not hand-written core.
GENERATED_DIRS: frozenset[str] = frozenset({"grpc_gen"})

#: Frozen baseline: ``(module path relative to src/, imported module)``.
#: This set only ever shrinks.
ALLOWED_VENDOR_IMPORTS: frozenset[tuple[str, str]] = frozenset(
    {
        ("bernstein/core/security/secrets.py", "boto3"),
        ("bernstein/core/security/secrets_broker.py", "boto3"),
        ("bernstein/core/security/secrets_broker.py", "google.cloud"),
        ("bernstein/core/security/secrets_broker.py", "keyring"),
        ("bernstein/core/security/vault/backend_keyring.py", "keyring"),
        ("bernstein/core/security/vault_injector.py", "boto3"),
        ("bernstein/core/storage/sinks/azure_blob.py", "azure.core"),
        ("bernstein/core/storage/sinks/azure_blob.py", "azure.storage"),
        ("bernstein/core/storage/sinks/gcs.py", "google.api_core"),
        ("bernstein/core/storage/sinks/gcs.py", "google.cloud"),
        ("bernstein/core/storage/sinks/s3.py", "boto3"),
        ("bernstein/core/storage/sinks/s3.py", "botocore"),
    }
)


def _vendor_imports(root: Path = CORE, *, relative_to: Path | None = None) -> set[tuple[str, str]]:
    """Return every vendor SDK import site under ``root``."""
    base = relative_to if relative_to is not None else CORE.parents[1]
    found: set[tuple[str, str]] = set()
    for path in sorted(root.rglob("*.py")):
        if GENERATED_DIRS & set(path.parts):
            continue
        rel = path.relative_to(base).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level:
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if name.split(".")[0] in VENDOR_SDK_ROOTS:
                    found.add((rel, name))
    return found


class TestCoreVendorSdkImports:
    def test_core_may_not_gain_a_vendor_sdk_import(self) -> None:
        """18. A new vendor SDK import anywhere under core fails this check."""
        added = sorted(_vendor_imports() - ALLOWED_VENDOR_IMPORTS)
        assert not added, (
            "bernstein.core gained a vendor SDK import: "
            f"{added}. Put the SDK behind a contract core calls -- see "
            "core/security/external_secret_store.py -- and register the "
            "implementation as a plugin instead."
        )

    def test_baseline_shrinks_when_a_site_leaves_core(self) -> None:
        """19. The allowlist has a ceiling: a stale entry fails too."""
        stale = sorted(ALLOWED_VENDOR_IMPORTS - _vendor_imports())
        assert not stale, f"ALLOWED_VENDOR_IMPORTS lists sites that no longer exist: {stale}. Remove them."

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ("import boto3\n", "boto3"),
            ("from google.cloud import secretmanager\n", "google.cloud"),
            ("import hvac as vault_client\n", "hvac"),
        ],
    )
    def test_detector_flags_a_synthetic_vendor_import(self, tmp_path, source: str, expected: str) -> None:
        """20. The check is not vacuous: a planted SDK import is found."""
        (tmp_path / "planted.py").write_text(source, encoding="utf-8")
        assert _vendor_imports(tmp_path, relative_to=tmp_path) == {("planted.py", expected)}

    def test_detector_ignores_a_relative_import_of_a_same_named_module(self, tmp_path) -> None:
        """21. A relative import is core's own module, not a vendor SDK."""
        (tmp_path / "planted.py").write_text("from .keyring import Backend\n", encoding="utf-8")
        assert _vendor_imports(tmp_path, relative_to=tmp_path) == set()

    def test_the_new_external_store_surface_is_sdk_free(self) -> None:
        """22. The contract this issue adds imports no vendor SDK itself."""
        new_surface = {
            "bernstein/core/security/external_secret_store.py",
            "bernstein/core/security/secret_store_registry.py",
        }
        offenders = sorted(site for site in _vendor_imports() if site[0] in new_surface)
        assert not offenders, offenders
