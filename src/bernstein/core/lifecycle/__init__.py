"""Lifecycle-hooks subsystem.

Provides a unified registry that fans events out to pluggy hook
implementations, Python callables, and shell scripts declared in
``bernstein.yaml``.

Historically ``bernstein.core.lifecycle`` resolved - via the in-tree
core-redirect finder - to :mod:`bernstein.core.tasks.lifecycle`, the
task/agent governance FSM. Turning it into a package (so we could
land new modules like :mod:`.hooks` and :mod:`.pluggy_bridge`) would
normally break that redirect because real packages win over meta-path
finders. We therefore install a compatibility shim below that merges
our new public surface into the original FSM module and remaps
``sys.modules`` so existing ``from bernstein.core.lifecycle import
transition_agent`` callers continue to resolve exactly as before,
including module-level attribute writes used by their tests.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # The ``sys.modules`` remap at the bottom of this file is invisible to
    # static analysis, so type checkers see this package rather than the FSM
    # module it aliases to and report the FSM's public names as missing.
    # Re-export them (explicit-alias form) so the checker agrees with what
    # ``from bernstein.core.lifecycle import ...`` actually resolves to at
    # runtime. Type-only: nothing here executes.
    from bernstein.core.tasks.lifecycle import (
        IllegalTransitionError as IllegalTransitionError,
    )
    from bernstein.core.tasks.lifecycle import (
        transition_agent as transition_agent,
    )
    from bernstein.core.tasks.lifecycle import (
        transition_task as transition_task,
    )

from bernstein.core.lifecycle.hooks import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_STDOUT_BYTES,
    HookFailure,
    HookRegistry,
    LifecycleContext,
    LifecycleEvent,
)
from bernstein.core.lifecycle.pluggy_bridge import (
    LifecycleHookSpec,
    apply_hooks_to_existing_system,
)

# ---------------------------------------------------------------------------
# Backwards-compatibility shim: merge our symbols into the historical
# ``bernstein.core.tasks.lifecycle`` module and alias ``sys.modules``.
# ---------------------------------------------------------------------------
from bernstein.core.tasks import lifecycle as _task_lifecycle

_hook_exports: dict[str, object] = {
    "DEFAULT_TIMEOUT_SECONDS": DEFAULT_TIMEOUT_SECONDS,
    "MAX_STDOUT_BYTES": MAX_STDOUT_BYTES,
    "HookFailure": HookFailure,
    "HookRegistry": HookRegistry,
    "LifecycleContext": LifecycleContext,
    "LifecycleEvent": LifecycleEvent,
    "LifecycleHookSpec": LifecycleHookSpec,
    "apply_hooks_to_existing_system": apply_hooks_to_existing_system,
}
# Only copy names that don't already exist on the target, to avoid
# shadowing the FSM's own ``LifecycleEvent`` type for legacy callers.
for _name, _value in _hook_exports.items():
    if not hasattr(_task_lifecycle, _name):
        setattr(_task_lifecycle, _name, _value)

# Preserve package discovery for ``bernstein.core.lifecycle.hooks`` and
# ``bernstein.core.lifecycle.pluggy_bridge`` after the alias - the real
# package object carries ``__path__``; copy it onto the FSM module so
# import of our submodules still works post-alias.
if not hasattr(_task_lifecycle, "__path__"):
    _task_lifecycle.__path__ = __path__  # type: ignore[attr-defined]

# Re-point ``bernstein.core.lifecycle`` at the FSM module. Any future
# imports (``import bernstein.core.lifecycle``) receive the FSM module
# with our hook surface merged in.
sys.modules[__name__] = _task_lifecycle  # type: ignore[assignment]
