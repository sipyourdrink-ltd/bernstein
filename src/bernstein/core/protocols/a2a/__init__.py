"""A2A protocol modules (audit-191 split).

Re-exports from ``.a2a`` so ``from bernstein.core.protocols.a2a import X``
keeps working for callers that previously imported from the ``a2a.py``
module (which now lives at ``bernstein.core.protocols.a2a.a2a``).
"""

from bernstein.core.protocols.a2a.a2a import *  # noqa: F403
