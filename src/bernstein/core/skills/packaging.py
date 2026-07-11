"""Receipt-backed installs for the packaged bernstein agent skill (#2369).

Bernstein ships a cross-vendor ``bernstein-run`` skill (open ``SKILL.md``
format) and a plugin bundle so agent sessions can drive orchestration
without a separate shell. An install into a host's skill directory is not
fire-and-forget: it produces the same chain-verifiable install receipt the
signed skills catalog already emits (:mod:`bernstein.core.skills.provenance`).

* :func:`tree_content_hash` computes a deterministic content address over a
  directory tree - a canonical JSON manifest of ``(relpath, sha256)`` pairs,
  hashed - so two byte-identical installs share one identity regardless of
  location.
* :func:`install_packaged_skill` copies the bundled skill into a host skill
  directory (or, with ``record_only``, anchors a tree the host already
  installed, e.g. a plugin checkout) and writes an
  :class:`~bernstein.core.skills.provenance.InstallReceipt` anchored in the
  ``skills`` lineage spine plus a ``plugin.install_receipt`` audit-chain
  event.
* :func:`verify_packaged_install` recomputes the installed tree's content
  address and receipt. Because receipts are content-addressed, any byte
  change in the installed tree resolves to a different address with no
  receipt - tamper and unattested content are the same verdict.

Determinism: identical tree bytes, ``install_id``, and ``timestamp``
produce byte-identical receipts and identical spine anchors in fresh
workdirs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from bernstein.core.skills.provenance import (
    InstallReceipt,
    InstallVerifyResult,
    verify_install,
    write_install_receipt,
)

logger = logging.getLogger(__name__)

#: Directory name of the bundled cross-vendor skill.
PACKAGED_SKILL_NAME = "bernstein-run"

#: Manifest filenames probed (in order) when computing the manifest hash of
#: an installed tree. ``SKILL.md`` covers skill installs; the plugin
#: manifests cover a plugin checkout recorded post hoc.
_MANIFEST_CANDIDATES = ("SKILL.md", ".plugin/plugin.json", "plugin.json")

#: Default skill-directory location per supported agent host, as
#: ``(project-relative, home-relative)`` parents. Overridable with an
#: explicit destination for hosts that scan a different directory.
_HOST_SKILL_PARENTS: dict[str, tuple[str, str]] = {
    "claude": (".claude/skills", ".claude/skills"),
    "codex": (".codex/skills", ".codex/skills"),
    "cursor": (".cursor/skills", ".cursor/skills"),
    "gemini": (".gemini/skills", ".gemini/skills"),
    "copilot": (".github/skills", ".copilot/skills"),
}


class PackagedInstallError(RuntimeError):
    """Raised when a packaged skill install or record cannot proceed."""


# ---------------------------------------------------------------------------
# Bundled asset resolution
# ---------------------------------------------------------------------------


def packaged_asset_root() -> Path:
    """Return the root of the bundled agent-plugin assets.

    Resolution order:

    1. The wheel-bundled copy under ``bernstein/_default_templates/agent_plugin``
       (force-included at build time).
    2. The development-checkout root (the repository root, which carries the
       canonical ``skills/`` / ``commands/`` / ``agents/`` / ``rules/`` tree).

    Raises:
        PackagedInstallError: When neither location exists.
    """
    package_root = Path(__file__).resolve().parents[2]
    bundled = package_root / "_default_templates" / "agent_plugin"
    if (bundled / "skills" / PACKAGED_SKILL_NAME).is_dir():
        return bundled
    # Development checkout: src/bernstein/core/skills -> repo root.
    repo_root = Path(__file__).resolve().parents[4]
    if (repo_root / "skills" / PACKAGED_SKILL_NAME).is_dir():
        return repo_root
    raise PackagedInstallError(
        f"bundled agent-plugin assets not found; expected {bundled} or {repo_root / 'skills' / PACKAGED_SKILL_NAME}"
    )


def packaged_skill_dir() -> Path:
    """Return the bundled ``bernstein-run`` skill directory."""
    return packaged_asset_root() / "skills" / PACKAGED_SKILL_NAME


# ---------------------------------------------------------------------------
# Content addressing
# ---------------------------------------------------------------------------


def _iter_regular_files(root: Path) -> list[Path]:
    """Return every regular file under *root*, sorted by posix relpath.

    Symlinks are rejected: a content address must cover the bytes an agent
    host will actually read, and a link pointing outside the tree would make
    the address depend on unhashed content.
    """
    files: list[Path] = []
    for path in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
        if path.is_symlink():
            raise PackagedInstallError(f"refusing to hash symlink inside skill tree: {path}")
        if path.is_file():
            files.append(path)
    return files


def tree_content_hash(root: Path) -> str:
    """Return the deterministic content address of a directory tree.

    The address is ``sha256:<hex>`` over the canonical JSON manifest
    ``[[relpath, sha256(content)], ...]`` with entries sorted by posix
    relpath - byte-sensitive, name-sensitive, and location-independent.

    Raises:
        PackagedInstallError: When *root* is not a directory, is empty, or
            contains a symlink.
    """
    if not root.is_dir():
        raise PackagedInstallError(f"not a directory: {root}")
    entries: list[list[str]] = []
    for path in _iter_regular_files(root):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        entries.append([path.relative_to(root).as_posix(), digest])
    if not entries:
        raise PackagedInstallError(f"directory tree is empty: {root}")
    manifest = json.dumps(entries, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(manifest).hexdigest()


def manifest_hash_for(root: Path) -> tuple[str, str]:
    """Return ``(manifest_relpath, sha256_hex)`` for the tree at *root*.

    Probes :data:`_MANIFEST_CANDIDATES` in order. The manifest hash is what
    the install receipt binds to, so a manifest edit is detectable even when
    the rest of the tree is untouched.

    Raises:
        PackagedInstallError: When no manifest candidate exists.
    """
    for candidate in _MANIFEST_CANDIDATES:
        path = root / candidate
        if path.is_file():
            return candidate, hashlib.sha256(path.read_bytes()).hexdigest()
    raise PackagedInstallError(f"no manifest file ({', '.join(_MANIFEST_CANDIDATES)}) under {root}")


# ---------------------------------------------------------------------------
# Host destinations
# ---------------------------------------------------------------------------


def host_skill_parent(
    host: str,
    scope: str,
    *,
    workdir: Path,
    home: Path | None = None,
) -> Path:
    """Return the default skills parent directory for *host* and *scope*.

    Args:
        host: One of :data:`supported_hosts`.
        scope: ``project`` (relative to *workdir*) or ``user`` (relative to
            the home directory).
        workdir: Project root for project-scoped installs.
        home: Home-directory override (tests); defaults to ``Path.home()``.

    Raises:
        PackagedInstallError: On an unknown host or scope.
    """
    parents = _HOST_SKILL_PARENTS.get(host)
    if parents is None:
        raise PackagedInstallError(f"unknown host {host!r}; expected one of {', '.join(sorted(_HOST_SKILL_PARENTS))}")
    project_rel, user_rel = parents
    if scope == "project":
        return workdir / project_rel
    if scope == "user":
        return (home if home is not None else Path.home()) / user_rel
    raise PackagedInstallError(f"unknown scope {scope!r}; expected project or user")


def supported_hosts() -> tuple[str, ...]:
    """Return the host names with a default skills directory."""
    return tuple(sorted(_HOST_SKILL_PARENTS))


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PackagedInstallOutcome:
    """Result of one receipt-backed packaged install."""

    dest: Path
    skill_hash: str
    manifest_hash: str
    manifest_path: str
    install_id: str
    spine_anchor: str
    copied: bool


def _copy_tree(source: Path, dest: Path) -> None:
    """Copy the regular files of *source* into *dest* with containment checks.

    Every destination path is resolved and required to stay inside the
    resolved destination root, so a crafted relative path can never escape
    the install directory.
    """
    dest.mkdir(parents=True, exist_ok=True)
    dest_resolved = dest.resolve()
    for src_file in _iter_regular_files(source):
        rel = src_file.relative_to(source)
        target = (dest_resolved / rel).resolve()
        if not target.is_relative_to(dest_resolved):
            raise PackagedInstallError(f"install path escapes destination: {rel.as_posix()}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src_file, target)


def install_packaged_skill(
    *,
    workdir: Path,
    dest: Path,
    hmac_key: bytes,
    install_id: str,
    timestamp: int,
    source: Path | None = None,
    host: str = "dest",
    scope: str = "dest",
    force: bool = False,
    record_only: bool = False,
) -> PackagedInstallOutcome:
    """Install the packaged skill into *dest* and anchor an install receipt.

    Two modes:

    * Copy mode (default): copy *source* (the bundled skill when omitted)
      into *dest*. A pre-existing *dest* whose content address differs from
      the source is refused unless ``force`` is set; an identical *dest* is
      re-anchored without copying.
    * ``record_only``: hash the tree already at *dest* (e.g. a plugin
      checkout an agent host installed) and anchor it without writing to it.

    The receipt's canonical bytes are the artifact the ``skills`` lineage
    spine hashes; the returned anchor is that spine entry hash. The install
    is also mirrored into the HMAC audit chain as a
    ``plugin.install_receipt`` event. If anchoring fails the install raises:
    an unattested tree is surfaced immediately rather than discovered at
    verify time (re-run with ``record_only`` after fixing the workspace).

    Args:
        workdir: Project root; receipts land under ``.sdd/skills/receipts/``.
        dest: Destination skill directory (the tree that gets hashed).
        hmac_key: Audit-chain HMAC key tagging spine and chain entries.
        install_id: Per-install identifier recorded in the receipt.
        timestamp: Integer timestamp recorded in the receipt (caller-chosen
            so identical fixtures anchor byte-identically).
        source: Tree to copy; defaults to the bundled skill.
        host: Host label recorded in the audit event.
        scope: Scope label recorded in the audit event.
        force: Overwrite a divergent pre-existing *dest* in copy mode.
        record_only: Anchor *dest* as-is without copying.

    Returns:
        A :class:`PackagedInstallOutcome` with the content address, manifest
        hash, and spine anchor.

    Raises:
        PackagedInstallError: Missing tree, divergent destination without
            ``force``, or path escape.
    """
    copied = False
    if record_only:
        if not dest.is_dir():
            raise PackagedInstallError(f"record-only install requires an existing tree at {dest}")
    else:
        src = source if source is not None else packaged_skill_dir()
        src_hash = tree_content_hash(src)
        if dest.is_dir() and any(dest.iterdir()):
            dest_hash = tree_content_hash(dest)
            if dest_hash != src_hash and not force:
                raise PackagedInstallError(
                    f"destination {dest} already contains a different tree "
                    f"({dest_hash[:19]}... vs {src_hash[:19]}...); pass force to overwrite"
                )
            if dest_hash != src_hash:
                shutil.rmtree(dest)
                _copy_tree(src, dest)
                copied = True
        else:
            _copy_tree(src, dest)
            copied = True

    skill_hash = tree_content_hash(dest)
    manifest_path, manifest_hash = manifest_hash_for(dest)

    receipt = InstallReceipt(
        skill_hash=skill_hash,
        manifest_hash=manifest_hash,
        install_id=install_id,
        timestamp=timestamp,
    )
    lineage_root = workdir / ".sdd" / "lineage"
    anchor = write_install_receipt(
        workdir=workdir,
        lineage_root=lineage_root,
        hmac_key=hmac_key,
        receipt=receipt,
    )
    _record_chain_event(
        workdir=workdir,
        hmac_key=hmac_key,
        skill_hash=skill_hash,
        manifest_hash=manifest_hash,
        install_id=install_id,
        spine_anchor=anchor,
        host=host,
        scope=scope,
        dest=dest,
    )
    return PackagedInstallOutcome(
        dest=dest,
        skill_hash=skill_hash,
        manifest_hash=manifest_hash,
        manifest_path=manifest_path,
        install_id=install_id,
        spine_anchor=anchor,
        copied=copied,
    )


def _record_chain_event(
    *,
    workdir: Path,
    hmac_key: bytes,
    skill_hash: str,
    manifest_hash: str,
    install_id: str,
    spine_anchor: str,
    host: str,
    scope: str,
    dest: Path,
) -> None:
    """Mirror the install receipt into the HMAC audit chain."""
    from bernstein.core.security.audit_chain import (
        AuditChainStore,
        record_plugin_install_receipt,
    )

    chain = AuditChainStore(workdir / ".sdd" / "audit", key=hmac_key)
    record_plugin_install_receipt(
        chain=chain,
        skill_hash=skill_hash,
        manifest_hash=manifest_hash,
        install_id=install_id,
        spine_anchor=spine_anchor,
        host=host,
        scope=scope,
        dest=str(dest),
    )


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


def verify_packaged_install(
    *,
    workdir: Path,
    dest: Path,
    hmac_key: bytes,
) -> InstallVerifyResult:
    """Recompute the installed tree's receipt and verify its anchor.

    The tree at *dest* is re-hashed; the recomputed content address selects
    the receipt, and :func:`bernstein.core.skills.provenance.verify_install`
    checks the install spine and the manifest hash. A tampered tree resolves
    to a content address with no receipt - the tamper verdict is structural,
    not a comparison an attacker can update.

    Args:
        workdir: Project root holding ``.sdd/``.
        dest: The installed skill / plugin directory.
        hmac_key: Audit-chain HMAC key.

    Returns:
        An :class:`~bernstein.core.skills.provenance.InstallVerifyResult`.
    """
    try:
        skill_hash = tree_content_hash(dest)
        _, manifest_hash = manifest_hash_for(dest)
    except PackagedInstallError as exc:
        return InstallVerifyResult(ok=False, reason=str(exc))
    return verify_install(
        workdir=workdir,
        lineage_root=workdir / ".sdd" / "lineage",
        hmac_key=hmac_key,
        skill_hash=skill_hash,
        installed_manifest_hash=manifest_hash,
    )


__all__ = [
    "PACKAGED_SKILL_NAME",
    "PackagedInstallError",
    "PackagedInstallOutcome",
    "host_skill_parent",
    "install_packaged_skill",
    "manifest_hash_for",
    "packaged_asset_root",
    "packaged_skill_dir",
    "supported_hosts",
    "tree_content_hash",
    "verify_packaged_install",
]
