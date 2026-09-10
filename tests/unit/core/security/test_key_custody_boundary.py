"""The signing key-custody boundary: one module, and a guard that keeps it one.

`KMSAdapter` and its backends were written for lineage v2 and lived in
`core/security/lineage_kms.py`, a module whose name says which subsystem owns
it. Every other signing surface in the tree therefore did the obvious thing and
called `serialization.load_pem_private_key(..., password=None)` itself. At the
time this guard was written that was true in 26 files across unrelated
subsystems, so "which custody backend holds the signing key" had 26 separate
answers and moving one of them to an HSM moved exactly one.

Two properties are pinned here:

* the protocol and its backends are importable from a neutral module and are
  the *same objects* as the names `lineage_kms` still exports, so the move
  costs no importer anything;
* the set of files that load private key material directly can only shrink.
  New direct loads fail; a migrated file that is still recorded as an
  exception fails too, so the list cannot drift away from the tree.

No call site is migrated here. The list below is the starting state, recorded
so that later work has something to subtract from.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

#: Scans the source tree rather than importing it, so no diff produces an
#: import edge to this file. The marker puts it in every pull request's
#: affected slice instead of only the merge group (#5428).
pytestmark = pytest.mark.whole_tree_guard

REPO_ROOT = Path(__file__).resolve().parents[4]
SOURCE_ROOT = REPO_ROOT / "src" / "bernstein"

#: Names whose appearance means a module is holding raw private key material.
_DIRECT_KEY_LOAD_NAMES = frozenset({"load_pem_private_key", "from_private_bytes"})

#: The custody boundary itself. These modules load key material because that is
#: their job -- they are what every other site is meant to route through.
_CUSTODY_BOUNDARY = frozenset(
    {
        "src/bernstein/core/persistence/lineage_signer.py",
    },
)

#: Signing surfaces that still load key material themselves, recorded as of the
#: commit that introduced this guard. Every entry is a site to migrate to the
#: custody boundary; deleting an entry is the point. Do not add to this set --
#: a new signing surface takes its signer from `core/security/key_custody.py`.
_KNOWN_DIRECT_KEY_LOAD_SITES = frozenset(
    {
        "src/bernstein/bridges/openclaw_gateway.py",
        "src/bernstein/cli/commands/receipt_cmd.py",
        "src/bernstein/cli/commands/supervisor_cmd.py",
        "src/bernstein/core/admission/engine.py",
        "src/bernstein/core/chat/drivers/discord.py",
        "src/bernstein/core/chat/drivers/slack.py",
        "src/bernstein/core/chat/drivers/teams.py",
        "src/bernstein/core/distribution/customer_countersign.py",
        "src/bernstein/core/evidence/bundle.py",
        "src/bernstein/core/identity/grants.py",
        "src/bernstein/core/identity/http_signing.py",
        "src/bernstein/core/interop/a2a_card.py",
        "src/bernstein/core/lineage/identity.py",
        "src/bernstein/core/observability/trust_record.py",
        "src/bernstein/core/orchestration/sla_monitor.py",
        "src/bernstein/core/protocols/a2a/agntcy_ads.py",
        "src/bernstein/core/routes/well_known.py",
        "src/bernstein/core/sandbox/pool_enrolment.py",
        "src/bernstein/core/sandbox/selection_receipt.py",
        "src/bernstein/core/security/agent_card_signer.py",
        "src/bernstein/core/security/capability_tokens.py",
        "src/bernstein/core/security/install_key.py",
        "src/bernstein/core/security/sigstore_attestation.py",
        "src/bernstein/core/skills/catalog/signature.py",
        "src/bernstein/core/volunteer/lease_store.py",
        "src/bernstein/github_app/app.py",
    },
)


def _loads_private_key_directly(tree: ast.Module) -> bool:
    """Return whether *tree* names a private-key loader anywhere.

    Attribute access (`serialization.load_pem_private_key`), a bare name after
    a `from ... import`, and the import itself all count. Strings and comments
    do not, which is why this reads the parsed tree rather than the text: a
    docstring that mentions the loader is documentation, not custody.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in _DIRECT_KEY_LOAD_NAMES:
            return True
        if isinstance(node, ast.Name) and node.id in _DIRECT_KEY_LOAD_NAMES:
            return True
        if isinstance(node, ast.alias) and node.name in _DIRECT_KEY_LOAD_NAMES:
            return True
    return False


def _direct_key_load_files() -> set[str]:
    """Return repo-relative paths of shipped modules that load key material."""
    found: set[str] = set()
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:  # pragma: no cover - a parse failure is another test's problem
            continue
        if _loads_private_key_directly(tree):
            found.add(path.relative_to(REPO_ROOT).as_posix())
    return found


def test_kms_adapter_importable_from_neutral_module_and_from_lineage_kms() -> None:
    """The custody names live in a neutral module and `lineage_kms` re-exports them.

    Importers wrote `from bernstein.core.security.lineage_kms import ...` when
    lineage was the only caller. Those imports must keep resolving to the very
    same objects, because `HSMKMSAdapter.__subclasses__()` is how a customer's
    integration is discovered: two class objects with one name would make a
    subclass of the wrong one invisible to config dispatch.
    """
    from bernstein.core.security import key_custody, lineage_kms

    shared = (
        "KMSAdapter",
        "FileBasedKMSAdapter",
        "EnvBasedKMSAdapter",
        "HSMKMSAdapter",
        "kms_adapter_from_config",
    )
    for name in shared:
        neutral = getattr(key_custody, name)
        legacy = getattr(lineage_kms, name)
        assert neutral is legacy, f"{name} is a different object in lineage_kms than in key_custody"

    assert set(lineage_kms.__all__) == set(key_custody.__all__), (
        "lineage_kms must re-export exactly the custody boundary's public names"
    )


def test_no_new_direct_private_key_load_sites() -> None:
    """A signing surface takes its key from the custody boundary, not from disk.

    Two failures, and they mean opposite things. An unrecorded file is a new
    site: route it through `core/security/key_custody.py` instead. A recorded
    file that no longer loads a key is a finished migration whose entry was
    left behind: delete the entry, in the same change, so the recorded set
    keeps describing the tree.
    """
    actual = _direct_key_load_files() - _CUSTODY_BOUNDARY

    unrecorded = sorted(actual - _KNOWN_DIRECT_KEY_LOAD_SITES)
    assert not unrecorded, (
        "these modules load private key material directly and are not recorded "
        "exceptions:\n  " + "\n  ".join(unrecorded) + "\n\nObtain a signer from "
        "bernstein.core.security.key_custody instead. An operator who moves the "
        "signing key into an HSM has to move every one of these separately, and "
        "`bernstein doctor` cannot report a custody backend for a key that is "
        "loaded ad hoc."
    )

    stale = sorted(_KNOWN_DIRECT_KEY_LOAD_SITES - actual)
    assert not stale, (
        "these modules are recorded as loading private key material but no "
        "longer do:\n  " + "\n  ".join(stale) + "\n\nRemove them from "
        "_KNOWN_DIRECT_KEY_LOAD_SITES. The recorded set is only useful while it "
        "matches the tree."
    )
