"""Backward-compat shim -- real module moved to bernstein.core.tasks.lifecycle."""

from bernstein.core.tasks.lifecycle import *  # noqa: F401,F403
from bernstein.core.tasks.lifecycle import _content_hash as _content_hash  # noqa: F401
from bernstein.core.tasks.lifecycle import __doc__ as _doc  # noqa: F401
