"""Deprecated v1 ``LineageRecorder`` - compatibility shim only.

The sealing logic this module used to own now lives in
:mod:`bernstein.core.lineage.signed_write`, the supported signed-write path
(:func:`~bernstein.core.lineage.signed_write.seal_write` and
:class:`~bernstein.core.lineage.signed_write.SignedLineageLog`). Nothing under
``src/`` constructs :class:`LineageRecorder` or imports it, and the deprecation
guard (``tests/unit/lineage/test_spine_deprecations.py``) fails CI if that
changes.

The class survives only so out-of-tree callers and existing test fixtures that
already hold a recorder keep working. It is a subclass of
:class:`SignedLineageLog` with no behaviour of its own, so a value produced here
is byte-identical to one produced by the supported path.

This module deliberately does **not** re-export ``seal_write``. Re-exporting the
sealing primitive from the deprecated module is exactly the bypass issue #2960
closes: it would let a signed write ride a deprecated import path without
tripping any guard. Import it from
:mod:`bernstein.core.lineage.signed_write` instead.
"""

from __future__ import annotations

import warnings
from typing import TYPE_CHECKING

from bernstein.core.lineage.signed_write import SignedLineageLog

if TYPE_CHECKING:
    from bernstein.core.lineage.store import LineageStore

__all__ = ["LineageRecorder"]


class LineageRecorder(SignedLineageLog):
    """Deprecated alias for :class:`SignedLineageLog`.

    Emits a :class:`DeprecationWarning` on construction. Use
    :class:`bernstein.core.lineage.signed_write.SignedLineageLog` (or the
    module-level :func:`~bernstein.core.lineage.signed_write.seal_write`) in new
    code; the on-disk bytes, hashes and signatures are identical either way.
    """

    def __init__(self, store: LineageStore, *, operator_hmac_key: bytes) -> None:
        warnings.warn(
            "LineageRecorder is deprecated; use "
            "bernstein.core.lineage.signed_write.SignedLineageLog (or seal_write) instead",
            DeprecationWarning,
            stacklevel=2,
        )
        super().__init__(store, operator_hmac_key=operator_hmac_key)
