"""Directory provisioning adapters.

The mapping from an external directory's provisioning resources onto Bernstein
agent principals lives here, outside ``bernstein.core``: core owns the
principal lifecycle chain and knows nothing about directories.
"""

from bernstein.adapters.directory import scim

__all__ = ["scim"]
