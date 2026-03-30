"""Automatic API key rotation with leak detection and secrets manager integration.

Rotates API keys on a configurable schedule. Detects compromised keys via
fingerprint matching and revokes them immediately (or per policy).

Usage in bernstein.yaml::

    key_rotation:
      interval: 30d
      on_leak: revoke_immediately
      secrets_provider: vault
      secrets_path: secret/bernstein
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from bernstein.core.secrets import (
    SecretsConfig,
    SecretsError,
    _create_provider,
    invalidate_cache,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LeakPolicy = Literal["revoke_immediately", "revoke_after_rotation", "alert_only"]

# Default leak patterns: common API key prefixes that should never appear in logs.
_DEFAULT_LEAK_PATTERNS: list[str] = [
    r"sk-ant-[a-zA-Z0-9\-_]{20,}",  # Anthropic
    r"sk-[a-zA-Z0-9]{20,}",  # OpenAI-style
    r"gsk_[a-zA-Z0-9]{20,}",  # Groq
    r"AIza[a-zA-Z0-9\-_]{35}",  # Google
]


def _parse_interval(raw: str | int) -> int:
    """Parse a human-friendly interval string into seconds.

    Supports suffixes: s (seconds), m (minutes), h (hours), d (days).
    Plain integers are treated as seconds.

    Args:
        raw: Interval like ``"30d"``, ``"24h"``, ``3600``, or ``"3600"``.

    Returns:
        Interval in seconds.

    Raises:
        ValueError: If the format is unrecognized.
    """
    if isinstance(raw, int):
        return raw

    raw = raw.strip().lower()
    if raw.isdigit():
        return int(raw)

    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    suffix = raw[-1]
    if suffix in multipliers and raw[:-1].isdigit():
        return int(raw[:-1]) * multipliers[suffix]

    raise ValueError(f"Invalid interval format: {raw!r} (use e.g. '30d', '24h', '60m', '3600')")


@dataclass(frozen=True)
class KeyRotationConfig:
    """Configuration for automatic API key rotation.

    Attributes:
        interval_seconds: How often to rotate keys (default 30 days).
        on_leak: What to do when a leaked key is detected.
        secrets_provider: Provider type for fetching new keys (vault, aws, 1password).
        secrets_path: Provider-specific path to the secrets store.
        leak_patterns: Regex patterns that identify leaked keys in text.
        state_dir: Directory for persisting rotation state.
    """

    interval_seconds: int = 2592000  # 30 days
    on_leak: LeakPolicy = "revoke_immediately"
    secrets_provider: str | None = None
    secrets_path: str | None = None
    leak_patterns: list[str] = field(default_factory=list)
    state_dir: str = ".sdd/key_rotation"


# ---------------------------------------------------------------------------
# Key state tracking
# ---------------------------------------------------------------------------


class KeyState(StrEnum):
    """Lifecycle states for a managed API key."""

    ACTIVE = "active"
    ROTATING = "rotating"
    REVOKED = "revoked"
    EXPIRED = "expired"


@dataclass
class ManagedKey:
    """A tracked API key with lifecycle metadata.

    Attributes:
        key_id: Unique identifier for this key entry.
        env_var: Environment variable name (e.g. ANTHROPIC_API_KEY).
        state: Current lifecycle state.
        created_at: Unix timestamp when the key was registered.
        rotated_at: Unix timestamp of last rotation (None if never rotated).
        revoked_at: Unix timestamp when revoked (None if active).
        fingerprint: SHA-256 hash prefix for safe identification without storing the value.
        revoke_reason: Why the key was revoked (if applicable).
    """

    key_id: str
    env_var: str
    state: KeyState
    created_at: float
    rotated_at: float | None = None
    revoked_at: float | None = None
    fingerprint: str = ""
    revoke_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict."""
        return {
            "key_id": self.key_id,
            "env_var": self.env_var,
            "state": self.state.value,
            "created_at": self.created_at,
            "rotated_at": self.rotated_at,
            "revoked_at": self.revoked_at,
            "fingerprint": self.fingerprint,
            "revoke_reason": self.revoke_reason,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ManagedKey:
        """Deserialize from a dict."""
        return cls(
            key_id=str(d["key_id"]),
            env_var=str(d["env_var"]),
            state=KeyState(d["state"]),
            created_at=float(d["created_at"]),
            rotated_at=d.get("rotated_at"),
            revoked_at=d.get("revoked_at"),
            fingerprint=str(d.get("fingerprint", "")),
            revoke_reason=str(d.get("revoke_reason", "")),
        )


def _fingerprint(value: str) -> str:
    """Generate a short fingerprint from a secret value.

    Uses SHA-256 prefix — safe to store, impossible to reverse.

    Args:
        value: The secret value.

    Returns:
        First 16 hex characters of the SHA-256 hash.
    """
    return hashlib.sha256(value.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Key Rotation Manager
# ---------------------------------------------------------------------------


class KeyRotationManager:
    """Manages API key lifecycle: registration, rotation, leak detection, revocation.

    Keys are tracked in memory and persisted to disk as JSON.
    New keys are fetched from the configured secrets provider on rotation.
    """

    def __init__(
        self,
        config: KeyRotationConfig,
        secrets_config: SecretsConfig | None = None,
    ) -> None:
        self._config = config
        self._secrets_config = secrets_config
        self._keys: dict[str, ManagedKey] = {}
        self._lock = threading.Lock()
        self._leak_patterns = [
            re.compile(p)
            for p in (config.leak_patterns if config.leak_patterns else _DEFAULT_LEAK_PATTERNS)
        ]
        self._state_path = Path(config.state_dir) / "state.json"
        self._load_state()

    @property
    def config(self) -> KeyRotationConfig:
        """Return the rotation configuration."""
        return self._config

    def register_key(self, env_var: str, current_value: str) -> ManagedKey:
        """Register an API key for rotation management.

        Args:
            env_var: Environment variable name (e.g. ``ANTHROPIC_API_KEY``).
            current_value: The current key value (used only for fingerprinting).

        Returns:
            The newly registered ManagedKey.
        """
        now = time.time()
        fp = _fingerprint(current_value)
        key_id = f"{env_var}:{fp[:8]}"

        key = ManagedKey(
            key_id=key_id,
            env_var=env_var,
            state=KeyState.ACTIVE,
            created_at=now,
            fingerprint=fp,
        )

        with self._lock:
            self._keys[key_id] = key
            self._save_state()

        logger.info("Registered key %s for rotation (fingerprint=%s)", key_id, fp)
        return key

    def get_active_keys(self) -> list[ManagedKey]:
        """Return all keys in ACTIVE state."""
        with self._lock:
            return [k for k in self._keys.values() if k.state == KeyState.ACTIVE]

    def get_all_keys(self) -> list[ManagedKey]:
        """Return all tracked keys regardless of state."""
        with self._lock:
            return list(self._keys.values())

    def check_rotation_needed(self) -> list[ManagedKey]:
        """Find active keys that are past their rotation interval.

        Returns:
            List of keys that need rotation.
        """
        now = time.time()
        interval = self._config.interval_seconds
        due: list[ManagedKey] = []

        with self._lock:
            for key in self._keys.values():
                if key.state != KeyState.ACTIVE:
                    continue
                last_rotation = key.rotated_at or key.created_at
                if now - last_rotation >= interval:
                    due.append(key)

        return due

    def rotate_key(self, key: ManagedKey) -> ManagedKey:
        """Rotate a key by fetching a new value from the secrets provider.

        The old key is marked as expired and a new active key is created.
        If no secrets provider is configured, raises SecretsError.

        Args:
            key: The key to rotate.

        Returns:
            The new ManagedKey with updated fingerprint and timestamps.

        Raises:
            SecretsError: If the secrets provider is unavailable or misconfigured.
        """
        if self._secrets_config is None:
            raise SecretsError("Cannot rotate: no secrets provider configured")

        with self._lock:
            key.state = KeyState.ROTATING

        # Fetch new secrets from the provider
        provider = _create_provider(self._secrets_config.provider)
        try:
            new_secrets = provider.fetch(self._secrets_config.path)
        except Exception as exc:
            # Revert to active on failure
            with self._lock:
                key.state = KeyState.ACTIVE
            raise SecretsError(f"Rotation fetch failed for {key.env_var}: {exc}") from exc

        new_value = new_secrets.get(key.env_var)
        if new_value is None and self._secrets_config.field_map:
            # Try field_map reverse lookup
            for secret_field, mapped_var in self._secrets_config.field_map.items():
                    if mapped_var == key.env_var and secret_field in new_secrets:
                        new_value = new_secrets[secret_field]
                        break

        if new_value is None:
            with self._lock:
                key.state = KeyState.ACTIVE
            raise SecretsError(
                f"Rotation failed: {key.env_var} not found in secrets provider response"
            )

        now = time.time()
        new_fp = _fingerprint(new_value)

        with self._lock:
            # Mark old key as expired
            key.state = KeyState.EXPIRED
            key.rotated_at = now

            # Create new active key
            new_key_id = f"{key.env_var}:{new_fp[:8]}"
            new_key = ManagedKey(
                key_id=new_key_id,
                env_var=key.env_var,
                state=KeyState.ACTIVE,
                created_at=now,
                fingerprint=new_fp,
            )
            self._keys[new_key_id] = new_key
            self._save_state()

        # Invalidate secrets cache so next load picks up the new value
        if self._secrets_config:
            invalidate_cache(self._secrets_config)

        # Update environment variable in current process
        os.environ[key.env_var] = new_value

        logger.info(
            "Rotated key %s -> %s (env=%s)",
            key.key_id,
            new_key_id,
            key.env_var,
        )
        return new_key

    def detect_leak(self, text: str) -> list[ManagedKey]:
        """Scan text for patterns that match active API key formats.

        Checks if any matched pattern's fingerprint corresponds to a managed key.

        Args:
            text: Text to scan (e.g. log output, agent response).

        Returns:
            List of managed keys whose fingerprints matched leaked values.
        """
        leaked: list[ManagedKey] = []
        active_fps = {}

        with self._lock:
            for key in self._keys.values():
                if key.state == KeyState.ACTIVE:
                    active_fps[key.fingerprint] = key

        for pattern in self._leak_patterns:
            for match in pattern.finditer(text):
                candidate = match.group(0)
                fp = _fingerprint(candidate)
                if fp in active_fps:
                    leaked.append(active_fps[fp])

        return leaked

    def handle_leak(self, text: str) -> list[ManagedKey]:
        """Detect leaks and apply the configured leak policy.

        Args:
            text: Text to scan for leaked keys.

        Returns:
            List of keys that were detected as leaked.
        """
        leaked = self.detect_leak(text)
        if not leaked:
            return []

        policy = self._config.on_leak

        for key in leaked:
            if policy == "revoke_immediately":
                self.revoke_key(key, reason="Leaked key detected in output")
                logger.warning("SECURITY: Key %s revoked immediately due to leak", key.key_id)
            elif policy == "revoke_after_rotation":
                try:
                    self.rotate_key(key)
                    self.revoke_key(key, reason="Leaked key rotated and revoked")
                    logger.warning("SECURITY: Key %s rotated and revoked due to leak", key.key_id)
                except SecretsError:
                    # If rotation fails, revoke anyway for safety
                    self.revoke_key(key, reason="Leaked key revoked (rotation failed)")
                    logger.error("SECURITY: Key %s revoked after failed rotation", key.key_id)
            elif policy == "alert_only":
                logger.warning("SECURITY: Potential key leak detected for %s (alert only)", key.key_id)

        return leaked

    def revoke_key(self, key: ManagedKey, reason: str = "manual") -> None:
        """Revoke a key, marking it as unusable.

        Args:
            key: The key to revoke.
            reason: Why the key is being revoked.
        """
        with self._lock:
            key.state = KeyState.REVOKED
            key.revoked_at = time.time()
            key.revoke_reason = reason
            self._save_state()

        # Remove from environment to prevent further use
        os.environ.pop(key.env_var, None)

        logger.info("Revoked key %s: %s", key.key_id, reason)

    def _save_state(self) -> None:
        """Persist key state to disk. Caller must hold self._lock."""
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        data = {kid: k.to_dict() for kid, k in self._keys.items()}
        tmp = self._state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self._state_path)

    def _load_state(self) -> None:
        """Load persisted key state from disk."""
        if not self._state_path.exists():
            return
        try:
            raw = json.loads(self._state_path.read_text())
            if isinstance(raw, dict):
                for kid, kdata in raw.items():
                    if isinstance(kdata, dict):
                        self._keys[kid] = ManagedKey.from_dict(kdata)
            logger.info("Loaded %d key(s) from %s", len(self._keys), self._state_path)
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.warning("Failed to load key rotation state: %s", exc)


# ---------------------------------------------------------------------------
# Background rotation scheduler
# ---------------------------------------------------------------------------


class KeyRotationScheduler:
    """Background thread that checks for and executes key rotations.

    Runs at a fraction of the rotation interval (every hour by default)
    to detect keys that are due for rotation.
    """

    def __init__(
        self,
        manager: KeyRotationManager,
        check_interval: float = 3600.0,
    ) -> None:
        self._manager = manager
        self._check_interval = check_interval
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Launch the background rotation check thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="key-rotation-scheduler",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "Key rotation scheduler started (check_interval=%.0fs, rotation_interval=%ds)",
            self._check_interval,
            self._manager.config.interval_seconds,
        )

    def stop(self) -> None:
        """Signal the background thread to exit."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        logger.info("Key rotation scheduler stopped")

    def _run(self) -> None:
        """Core check loop: find and rotate due keys."""
        while not self._stop_event.is_set():
            try:
                due = self._manager.check_rotation_needed()
                for key in due:
                    try:
                        self._manager.rotate_key(key)
                    except SecretsError as exc:
                        logger.error("Scheduled rotation failed for %s: %s", key.key_id, exc)
            except Exception as exc:
                logger.error("Key rotation check failed: %s", exc)

            self._stop_event.wait(timeout=self._check_interval)
