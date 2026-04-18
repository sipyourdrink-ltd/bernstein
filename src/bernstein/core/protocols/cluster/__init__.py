"""Cluster protocol modules (audit-191 split).

Re-exports from ``.cluster`` so ``from bernstein.core.protocols.cluster
import NodeRegistry`` keeps working for callers that previously imported
from the ``cluster.py`` module (now at
``bernstein.core.protocols.cluster.cluster``).
"""

from bernstein.core.protocols.cluster.cluster import *  # noqa: F403
