"""Persistent Ed25519 keystore for the A2A v1.0 agent-card signer.

The first iteration of the v1.0 ``/.well-known/agent.json`` route cached its
Ed25519 keypair in process memory - fine for one-shot integration tests but
not for production: every restart minted a fresh ``kid`` and broke verifiers
that had cached the old JWK.

This module persists the keypair under ``.bernstein/keys/`` with the same
PEM shapes ``agent_card_signer.generate_ed25519_keypair`` produces, plus a
24-hour rotation grace window so verifiers that fetched the previous JWKS
keep validating in-flight signatures while their HTTP cache (max-age=3600)
ages out.

Filesystem layout
-----------------

::

    .bernstein/keys/
        agent-card.ed25519       0600  PKCS#8 PEM private key (current)
        agent-card.ed25519.pub   0600  SPKI PEM public key  (current)
        archive/
            <iso-timestamp>/
                agent-card.ed25519       0600  rotated-out private
                agent-card.ed25519.pub   0600  rotated-out public
                rotated_at.txt           UTC ISO-8601 timestamp

The JWKS endpoint at ``/.well-known/agent.json/keys`` reads the current
public key plus any archived public key whose ``rotated_at`` falls within
the grace window (24h) so verifiers cached on the old key still validate.

Security notes
--------------

* The private file is created with ``os.O_EXCL`` so two concurrent first-run
  processes cannot race each other into overwriting a freshly minted key.
* The private file is forced to ``0o600`` after write - both at generation
  time and on every load (a load that finds wider-than-owner permissions
  raises so the operator notices the misconfiguration).
* No envelope encryption today; on-disk plaintext is the same shape as the
  ed25519 keypair the previous in-process cache held. Production deployments
  that want envelope encryption (KMS, sops, age) should layer it on top of
  the directory itself, not inside this module.
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import shutil
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from ..identity.http_signing import install_identity_keyid
from .agent_card_signer import generate_ed25519_keypair
from .audit_chain import EVENT_IDENTITY_ROTATION
from .key_custody import FileBasedKMSAdapter, KMSAdapter

__all__ = [
    "DEFAULT_GRACE_SECONDS",
    "DEFAULT_KEY_DIR",
    "AgentCardKeystore",
    "ArchivedKey",
]

logger = logging.getLogger(__name__)

#: Default rotation grace window. Verifiers that cached the previous JWKS
#: (Cache-Control: max-age=3600) get up to 24h to refresh and pick up the
#: new ``kid`` while the old ``kid`` keeps verifying.
DEFAULT_GRACE_SECONDS: int = 24 * 60 * 60

#: Default keystore directory, relative to the workdir.
DEFAULT_KEY_DIR: Path = Path(".bernstein/keys")

#: Required private-key permission bits. Anything more permissive than
#: owner-only is treated as a misconfiguration.
_PRIVATE_MODE_MASK: int = 0o077

_PRIVATE_FILENAME: str = "agent-card.ed25519"
_PUBLIC_FILENAME: str = "agent-card.ed25519.pub"
_ARCHIVE_DIRNAME: str = "archive"
_ROTATED_AT_FILENAME: str = "rotated_at.txt"


@dataclass(frozen=True, slots=True)
class ArchivedKey:
    """A previously-active keypair retained during the rotation grace window.

    The ``private_pem`` is loaded but never returned outside this module -
    the JWKS endpoint only needs ``public_pem`` to publish the legacy JWK.
    Keeping it on disk lets operators replay or audit the historic
    signature surface without re-deriving keys.
    """

    public_pem: bytes
    rotated_at: _dt.datetime
    #: Name of the ``archive/`` directory this key was read from. It carries
    #: the disambiguator ``_unique_archive_folder`` adds when two rotations
    #: share a second, which is the only thing that distinguishes them.
    archive_name: str = ""

    @property
    def kid(self) -> str:
        """Unique kid for the archived key, derived from its archive directory.

        The kid encodes the moment the key was rotated out so verifiers
        seeing both the current and archived key in the JWKS can route by
        ``kid`` without ambiguity.

        It is built from ``archive_name`` rather than from ``rotated_at``
        alone because ``rotated_at`` has one-second resolution. Two rotations
        inside one second used to be impossible to observe - the second
        overwrote the first - but once both survive they share a timestamp,
        and a kid derived from it would be shared too. The JWKS would then
        carry two entries with one ``kid`` and different keys, and
        ``a2a_conformance.resolve_jwk`` returns the first match: a verifier
        routing by that kid gets whichever sorted first, for the whole grace
        window.

        ``archive_name`` is unique by construction, so the kid is.
        """
        stamp = self.archive_name or self.rotated_at.strftime("%Y%m%dT%H%M%SZ")
        return f"agent-bernstein-orchestrator-{stamp}"


class AgentCardKeystore:
    """File-backed keystore for the orchestrator's agent-card signing key.

    Designed for the single-process Bernstein server: a per-instance lock
    serialises first-run generation and rotation. Multi-process deployments
    should either share the directory (and accept the small race on first
    boot - ``O_EXCL`` guarantees only one writer wins) or pre-provision the
    keypair before fanout.
    """

    def __init__(
        self,
        key_dir: Path | None = None,
        *,
        grace_seconds: int = DEFAULT_GRACE_SECONDS,
        clock: type[_dt.datetime] | None = None,
    ) -> None:
        """Bind a keystore to a directory.

        Args:
            key_dir: Directory holding the keypair. Defaults to
                ``.bernstein/keys`` under the current working directory.
            grace_seconds: How long an archived key stays in the JWKS after
                rotation. Defaults to 24 hours (matches the published
                ``Cache-Control: max-age=3600`` on the agent-card route by a
                comfortable margin).
            clock: Datetime class providing a ``now(tz=...)`` classmethod.
                Override for tests that need to advance time without
                touching real wall-clock state.
        """
        self._dir = (key_dir or DEFAULT_KEY_DIR).resolve()
        self._grace_seconds = max(0, grace_seconds)
        self._clock = clock or _dt.datetime
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    @property
    def directory(self) -> Path:
        """Resolved on-disk directory for the keypair."""
        return self._dir

    def load_or_generate(self) -> tuple[bytes, bytes]:
        """Return the active ``(private_pem, public_pem)`` keypair.

        On first call (or when the keystore directory is empty) generates a
        fresh keypair atomically with ``O_EXCL`` and ``0o600`` permissions.
        Subsequent calls re-read from disk so multiple processes converge
        on the same key - the in-memory cache is intentionally absent here;
        ``well_known.py`` is the cache layer.

        Raises:
            PermissionError: When the on-disk private key has wider-than
                owner-only permissions. Operators must tighten the bits
                before the orchestrator will use the key.
        """
        with self._lock:
            if not self._private_path.exists() or not self._public_path.exists():
                self._generate_atomic()
            return self._load_existing()

    def signer(self) -> KMSAdapter:
        """Return a custody-bounded signer over the active install-identity key.

        Callers that need a signature never handle the private PEM: the adapter
        reads the key file itself and only exposes ``sign`` and the public JWK.
        The ``kid`` is the install-identity key id derived from the public key.
        """
        _, public_pem = self.load_or_generate()
        return FileBasedKMSAdapter(self._private_path, kid=install_identity_keyid(public_pem))

    def list_archived(self) -> list[ArchivedKey]:
        """Return archived keys still inside the grace window.

        Old archive directories beyond the grace window are silently skipped
        (and may be GC'd by the operator out-of-band). The returned list is
        sorted oldest → newest so the JWKS publishes a stable order.
        """
        archive_dir = self._dir / _ARCHIVE_DIRNAME
        if not archive_dir.is_dir():
            return []

        cutoff = self._clock.now(tz=_dt.UTC) - _dt.timedelta(seconds=self._grace_seconds)
        out: list[ArchivedKey] = []
        for entry in sorted(archive_dir.iterdir()):
            if not entry.is_dir():
                continue
            rotated_at = self._read_rotated_at(entry)
            if rotated_at is None or rotated_at < cutoff:
                continue
            pub_path = entry / _PUBLIC_FILENAME
            if not pub_path.is_file():
                continue
            try:
                public_pem = pub_path.read_bytes()
            except OSError:  # pragma: no cover - filesystem flake
                logger.warning("agent-card keystore: unreadable archived public key at %s", pub_path)
                continue
            out.append(ArchivedKey(public_pem=public_pem, rotated_at=rotated_at, archive_name=entry.name))
        return out

    def rotate(self) -> tuple[bytes, bytes]:
        """Archive the current keypair and mint a new one.

        Returns the freshly-generated ``(private_pem, public_pem)``. The
        previous keypair is moved under ``archive/<isoformat>/`` together
        with a ``rotated_at.txt`` timestamp file so :meth:`list_archived`
        can read it deterministically.

        Archives whose grace window has closed are deleted here - see
        :meth:`_prune_expired_archives`. They stop being published the moment
        the window shuts, so keeping the files only retains retired *private*
        keys for no benefit.

        Side effects:
          Records an identity.rotation event to the audit chain with the
          new keyid (and old keyid if available).
          Deletes archive directories outside the grace window.
        """
        with self._lock:
            # Get old keyid if available
            old_keyid = None
            try:
                if self._private_path.exists() and self._public_path.exists():
                    _, old_public_pem = self._load_existing()
                    old_keyid = install_identity_keyid(old_public_pem)
            except Exception:
                # If we can't read the old key, continue anyway
                pass

            if self._private_path.exists() or self._public_path.exists():
                self._archive_existing()
            self._generate_atomic()
            # Drop what the grace window has already stopped publishing.
            # Done after archiving so the key retired by *this* rotation is
            # judged by the same cutoff as every other one.
            self._prune_expired_archives()

            # Record rotation to audit chain
            try:
                _, new_public_pem = self._load_existing()
                new_keyid = install_identity_keyid(new_public_pem)

                from bernstein.core.security.audit import AuditLog

                log = AuditLog(self._dir.parent / ".sdd" / "audit")
                log.log(
                    event_type=EVENT_IDENTITY_ROTATION,
                    actor="system",
                    resource_type="install_identity",
                    resource_id="key_rotation",
                    details={
                        "new_keyid": new_keyid,
                        **({"old_keyid": old_keyid} if old_keyid is not None else {}),
                    },
                )
            except Exception:
                # Best effort - audit failure shouldn't break key rotation
                pass

            return self._load_existing()

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    @property
    def _private_path(self) -> Path:
        return self._dir / _PRIVATE_FILENAME

    @property
    def _public_path(self) -> Path:
        return self._dir / _PUBLIC_FILENAME

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _generate_atomic(self) -> None:
        """Mint a fresh keypair, refusing to clobber an existing private key.

        Uses ``os.O_EXCL`` so two processes racing into first-run cannot
        both win - the loser sees ``FileExistsError`` and falls through to
        :meth:`_load_existing`.
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        private_pem, public_pem = generate_ed25519_keypair()

        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            fd = os.open(self._private_path, flags, 0o600)
        except FileExistsError:
            # Another writer beat us; abandon our generated material.
            return
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(private_pem)
        except Exception:
            # Roll back on any failure so subsequent runs can retry cleanly.
            self._private_path.unlink(missing_ok=True)
            raise

        # Force the bits even on platforms that ignored the umask.
        os.chmod(self._private_path, 0o600)

        # Public key may already exist (e.g. half-rolled-back rotation). It's
        # safe to overwrite - it always derives from the private one.
        # Use owner-only perms even on the public key - external consumers
        # fetch it via the JWKS HTTP endpoint, never directly off disk, so
        # there's no operational need to grant other local users FS access.
        self._public_path.write_bytes(public_pem)
        os.chmod(self._public_path, 0o600)

    def _load_existing(self) -> tuple[bytes, bytes]:
        """Return the on-disk keypair, asserting the private file is 0600."""
        try:
            stat = os.stat(self._private_path)
        except FileNotFoundError as exc:
            msg = f"agent-card private key missing at {self._private_path}"
            raise FileNotFoundError(msg) from exc

        if sys.platform != "win32" and (stat.st_mode & _PRIVATE_MODE_MASK):
            msg = (
                f"agent-card private key {self._private_path} has unsafe permissions "
                f"{stat.st_mode & 0o777:#o} - refusing to load. Run "
                f"'chmod 600 {self._private_path}' and retry."
            )
            raise PermissionError(msg)

        return self._private_path.read_bytes(), self._public_path.read_bytes()

    def _archive_existing(self) -> None:
        """Move the current keypair into ``archive/<utc-isoformat>/``."""
        rotated_at = self._clock.now(tz=_dt.UTC).replace(microsecond=0)
        # Folder name uses a filesystem-safe variant of the ISO timestamp,
        # disambiguated when two rotations land in the same second. Without
        # the suffix the second rotation reused the first one's directory and
        # ``Path.replace`` overwrote the keypair already archived there, so a
        # verifier still inside its grace window lost the key it was cached
        # on - silently, since the write succeeds.
        folder = self._unique_archive_folder(rotated_at)
        folder.mkdir(parents=True, exist_ok=True)

        if self._private_path.exists():
            archived_priv = folder / _PRIVATE_FILENAME
            self._private_path.replace(archived_priv)
            os.chmod(archived_priv, 0o600)

        if self._public_path.exists():
            archived_pub = folder / _PUBLIC_FILENAME
            self._public_path.replace(archived_pub)
            os.chmod(archived_pub, 0o600)

        (folder / _ROTATED_AT_FILENAME).write_text(rotated_at.isoformat() + "\n", encoding="utf-8")

    def _unique_archive_folder(self, rotated_at: _dt.datetime) -> Path:
        """Return an unused archive directory for a rotation at *rotated_at*.

        The stamp is second-resolution, so rapid rotations collide. The base
        name is kept unsuffixed for the common case - it is the shape
        ``_read_rotated_at`` falls back to parsing, and the overwhelming
        majority of archives keep it - and a ``-N`` suffix is added only when
        the directory is already taken.

        Args:
            rotated_at: The rotation instant, already truncated to seconds.

        Returns:
            A path under ``archive/`` that does not yet exist.
        """
        base = self._dir / _ARCHIVE_DIRNAME / rotated_at.strftime("%Y%m%dT%H%M%SZ")
        if not base.exists():
            return base
        suffix = 1
        while True:
            candidate = base.with_name(f"{base.name}-{suffix}")
            if not candidate.exists():
                return candidate
            suffix += 1

    def _prune_expired_archives(self) -> int:
        """Delete archive directories the grace window no longer covers.

        :meth:`list_archived` already ignores these, so nothing that is still
        being served is removed. Ignoring them is not the same as deleting
        them, though, and the gap is what matters: ``_archive_existing``
        moves the retired **private** key into the archive alongside the
        public one, so an entry that is never removed is a retired signing
        key kept on disk indefinitely. Every rotation adds another. An
        attacker able to force rotations both exhausts the disk and grows the
        set of private keys a later disk compromise yields.

        The cutoff is computed with the same expression :meth:`list_archived`
        uses, so the two cannot disagree about which entries are live - a
        prune that ran even slightly ahead of the publisher would delete a key
        the JWKS was still advertising.

        The two are not symmetric, though, and the asymmetry is worth stating:
        skipping is reversible and deleting is not. If the clock steps
        backwards between a prune and a later read - an NTP correction, a VM
        restored from a snapshot - an entry :meth:`list_archived` would have
        started publishing again is already gone. Sharing the expression makes
        the *decision* identical; it does not make the consequences identical.

        An entry whose rotation timestamp cannot be read is left alone. It is
        not published either, so it is only wasted space; deleting a
        directory this code cannot date is the one mistake here that destroys
        key material it did not understand.

        Returns:
            The number of archive directories removed.
        """
        archive_dir = self._dir / _ARCHIVE_DIRNAME
        if not archive_dir.is_dir():
            return 0

        cutoff = self._clock.now(tz=_dt.UTC) - _dt.timedelta(seconds=self._grace_seconds)
        removed = 0
        for entry in sorted(archive_dir.iterdir()):
            if not entry.is_dir():
                continue
            rotated_at = self._read_rotated_at(entry)
            if rotated_at is None:
                logger.warning(
                    "agent-card keystore: archive %s has no readable rotation timestamp; not published and not pruned",
                    entry,
                )
                continue
            if rotated_at >= cutoff:
                continue
            try:
                shutil.rmtree(entry)
            except OSError as exc:  # pragma: no cover - filesystem flake
                logger.warning("agent-card keystore: could not prune archive %s: %s", entry, exc)
                continue
            removed += 1
        if removed:
            logger.info("agent-card keystore: pruned %d archived keypair(s) past the grace window", removed)
        return removed

    @staticmethod
    def _read_rotated_at(archive_entry: Path) -> _dt.datetime | None:
        """Return the rotation timestamp recorded inside ``archive_entry``."""
        stamp_file = archive_entry / _ROTATED_AT_FILENAME
        if not stamp_file.is_file():
            # Fall back to the directory name (UTC stamp) for entries written
            # by older versions or moved by hand.
            try:
                # Strip the ``-N`` collision suffix ``_unique_archive_folder``
                # adds when two rotations share a second.
                stem = archive_entry.name.split("-", 1)[0]
                return _dt.datetime.strptime(stem, "%Y%m%dT%H%M%SZ").replace(
                    tzinfo=_dt.UTC,
                )
            except ValueError:
                return None
        try:
            text = stamp_file.read_text(encoding="utf-8").strip()
        except OSError:  # pragma: no cover - filesystem flake
            return None
        try:
            return _dt.datetime.fromisoformat(text)
        except ValueError:
            return None
