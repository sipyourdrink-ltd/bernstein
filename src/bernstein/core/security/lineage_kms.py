"""Compatibility re-export of the signing key-custody boundary.

The :class:`KMSAdapter` protocol and its backends were written for lineage v2
and lived here, which is why the rest of the tree read them as lineage's local
abstraction rather than as the project's signing boundary. They now live in
:mod:`bernstein.core.security.key_custody`, whose name carries no subsystem.

This module re-exports the same objects -- not copies -- so every existing
``from bernstein.core.security.lineage_kms import ...`` keeps working and keeps
resolving to the classes config dispatch knows about. Object identity matters
beyond tidiness here: ``kms_adapter_from_config`` discovers a customer's HSM
integration through :meth:`HSMKMSAdapter.__subclasses__`, and a second class
object under the same name would hide a subclass of the other one.

New code should import from :mod:`bernstein.core.security.key_custody`.
"""

from __future__ import annotations

from bernstein.core.security.key_custody import (
    EnvBasedKMSAdapter,
    FileBasedKMSAdapter,
    HSMKMSAdapter,
    KMSAdapter,
    kms_adapter_from_config,
)

__all__ = [
    "EnvBasedKMSAdapter",
    "FileBasedKMSAdapter",
    "HSMKMSAdapter",
    "KMSAdapter",
    "kms_adapter_from_config",
]
