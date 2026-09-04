"""Static guard: ``bernstein.core`` must not import a directory vendor SDK (issue #4970).

The bridge exists so a third party's release cadence stays outside our trust
boundary. That only holds while the vendor code lives in an adapter: one
``import msal`` inside ``bernstein.core`` puts it back in, and nothing at
runtime would notice. The guard is therefore static, and it is exercised
against a fixture tree as well as the real one so a scanner that silently
stops finding anything fails here first.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CORE_ROOT = REPO_ROOT / "src" / "bernstein" / "core"

#: Top-level distributions that speak a specific identity directory or IdP.
#: Adapters may depend on these; core may not. Cloud-storage and secret-store
#: SDKs are deliberately absent -- core already uses them for other reasons and
#: they are not directory clients.
DIRECTORY_VENDOR_SDKS: frozenset[str] = frozenset(
    {
        "adal",
        "auth0",
        "gssapi",
        "jumpcloud",
        "kerberos",
        "keycloak",
        "ldap",
        "ldap3",
        "msal",
        "msal_extensions",
        "msgraph",
        "msgraphcore",
        "okta",
        "onelogin",
        "pyad",
        "pysaml2",
        "python_keycloak",
        "python_ldap",
        "saml2",
        "scim2_client",
        "winkerberos",
        "workos",
    }
)

#: Dotted prefixes that are vendor directory clients even though their root
#: package is used elsewhere for unrelated services.
DIRECTORY_VENDOR_PREFIXES: tuple[str, ...] = (
    "azure.graphrbac",
    "azure.identity",
    "google.oauth2",
    "googleapiclient",
)


def _is_vendor_module(module: str) -> bool:
    root = module.split(".", 1)[0]
    if root in DIRECTORY_VENDOR_SDKS:
        return True
    return any(module == prefix or module.startswith(prefix + ".") for prefix in DIRECTORY_VENDOR_PREFIXES)


def find_directory_vendor_imports(root: Path) -> list[str]:
    """Return ``path:line:module`` for every directory vendor SDK import under ``root``.

    Function-local and ``TYPE_CHECKING`` imports count: an import that only
    runs on one code path is still a dependency of the module.
    """
    violations: list[str] = []
    for path in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - defensive
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module] if node.level == 0 and node.module else []
            else:
                continue
            violations.extend(
                f"{path.relative_to(root)}:{node.lineno}:{module}" for module in modules if _is_vendor_module(module)
            )
    return violations


def test_bernstein_core_imports_no_directory_vendor_sdk() -> None:
    """Core speaks the bridge contract only; vendor clients live in adapters."""
    assert find_directory_vendor_imports(CORE_ROOT) == []


def test_guard_detects_a_directory_vendor_sdk_import(tmp_path: Path) -> None:
    """The guard fires on a tree that does what core must not do."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "bad_top_level.py").write_text("import msal\n", encoding="utf-8")
    (tmp_path / "pkg" / "bad_from.py").write_text("from ldap3 import Server\n", encoding="utf-8")
    (tmp_path / "pkg" / "bad_lazy.py").write_text(
        "def connect() -> None:\n    from azure.identity import ClientSecretCredential\n",
        encoding="utf-8",
    )

    found = find_directory_vendor_imports(tmp_path)

    assert sorted(found) == [
        "pkg/bad_from.py:1:ldap3",
        "pkg/bad_lazy.py:2:azure.identity",
        "pkg/bad_top_level.py:1:msal",
    ]


def test_guard_does_not_fire_on_ordinary_imports(tmp_path: Path) -> None:
    """A guard that flags everything protects nothing."""
    (tmp_path / "ok.py").write_text(
        "import hashlib\nimport boto3\nfrom bernstein.core.security.rbac import RBACEnforcer\n",
        encoding="utf-8",
    )

    assert find_directory_vendor_imports(tmp_path) == []
