"""Kind-detecting dispatcher for a single ``bernstein verify <artefact>`` entry point (#5103).

Fifty-five Click groups each carry their own independently implemented
``verify`` command; nothing reads an artefact on disk and picks the right
one. This module is the first slice of the fix: a small registry of
``(kind, verifier)`` pairs plus a :func:`detect_kind` step that reads an
artefact and says which verifier applies, so ``dispatch_verify`` is a single
call site regardless of kind.

Two upstream pieces this module deliberately does not build:

* :class:`VerifyOutcome` is not
  :class:`~bernstein.core.verify_result.VerifyResult`. That type answers
  "did this receipt read back intact, and why" (``ok``, ``reason``,
  ``receipt``); a dispatcher additionally has to report which kind it routed
  to and what process exit code the CLI should return, neither of which
  ``VerifyResult`` carries. Reconciling the two -- whether the dispatcher
  wraps a ``VerifyResult`` or ``VerifyResult`` grows a kind -- is its own
  decision, not this slice's.
* the registry helper (duplicate-id detection at load, decision-record
  history) is #5104's scope; :data:`_REGISTRY` here is a plain dict with
  just enough duplicate-safety to be usable now. Adopting
  :class:`~bernstein.core.registry_guard.DuplicateGuard` for the duplicate
  half is follow-up under #5104, which also wants the load-time and
  decision-record behaviour the guard does not cover.

Kind detection is field-first with a sniffing fallback: an artefact whose
top-level JSON object carries a ``"kind"`` string matching a registered kind
uses that directly; failing that, each registered verifier's own ``sniff``
predicate is tried in registration order. Field-first alone is not viable
today because no existing artefact producer (the AI-BOM encoder, the result
receipt bundle) stamps a ``"kind"`` field yet -- sniffing is what makes
artefacts already on disk before this issue landed still detectable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

#: Exit code when no registered verifier's sniff predicate (nor an explicit
#: ``"kind"`` field) recognises the artefact. Distinct from both 0 (verified)
#: and 1 (a recognised kind that failed verification) -- and deliberately
#: not 2, which Click's own usage errors already use, so a caller cannot
#: mistake "no verifier recognises this" for a malformed invocation.
EXIT_UNKNOWN_KIND = 3


class DuplicateVerifierKindError(ValueError):
    """Raised when two verifiers try to register the same kind."""


@dataclass(frozen=True, slots=True)
class VerifyOutcome:
    """What a registered verifier reports back to the dispatcher.

    Every registered verifier returns this same shape, which is the property
    the dispatcher exists to guarantee: a caller checking ``ok``/``exit_code``
    never needs to know which of the 55 original commands used to own this
    artefact kind. Distinct from
    :class:`~bernstein.core.verify_result.VerifyResult`, which reports an
    offline receipt check rather than a routed dispatch -- see the module
    docstring.
    """

    kind: str
    ok: bool
    exit_code: int
    message: str
    detail: dict[str, Any] = field(default_factory=lambda: cast("dict[str, Any]", {}))


@dataclass(frozen=True, slots=True)
class VerifierSpec:
    """One registry entry: a kind name, a detector, and a verifier."""

    kind: str
    sniff: Callable[[Path, dict[str, Any] | None], bool]
    verify: Callable[[Path], VerifyOutcome]


#: kind -> VerifierSpec. Plain dict, not #5104's registry helper -- see the
#: module docstring.
_REGISTRY: dict[str, VerifierSpec] = {}


def register_verifier(spec: VerifierSpec) -> None:
    """Register *spec*. Raises if its kind is already registered."""
    if spec.kind in _REGISTRY:
        raise DuplicateVerifierKindError(f"a verifier is already registered for kind {spec.kind!r}")
    _REGISTRY[spec.kind] = spec


def unregister_all() -> None:
    """Clear the registry. Test-only: production code never calls this."""
    _REGISTRY.clear()


def registered_kinds() -> tuple[str, ...]:
    """The kinds currently registered, sorted for stable output."""
    return tuple(sorted(_REGISTRY))


def _read_json_object(path: Path) -> dict[str, Any] | None:
    """Best-effort JSON read. ``None`` for anything that is not a JSON object.

    Never raises: a directory, a binary file, or malformed JSON all mean
    "no field-based or structural information available", which sniffing
    predicates are expected to treat as "does not match".
    """
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    return cast("dict[str, Any]", raw)


def detect_kind(path: Path) -> str | None:
    """Return the registered kind *path* belongs to, or ``None`` if none matches."""
    payload = _read_json_object(path)
    if payload is not None:
        declared = payload.get("kind")
        if isinstance(declared, str) and declared in _REGISTRY:
            return declared
    for spec in _REGISTRY.values():
        if spec.sniff(path, payload):
            return spec.kind
    return None


def dispatch_verify(path: Path) -> VerifyOutcome:
    """Detect *path*'s kind and run its verifier.

    Returns a :class:`VerifyOutcome` with ``kind="unknown"`` and
    :data:`EXIT_UNKNOWN_KIND` when no registered verifier recognises the
    artefact, rather than raising -- the dispatcher's failure mode is a
    reportable outcome like any other, not an exception a caller must catch.
    """
    kind = detect_kind(path)
    if kind is None:
        return VerifyOutcome(
            kind="unknown",
            ok=False,
            exit_code=EXIT_UNKNOWN_KIND,
            message=(
                f"{path} does not match any registered verifier "
                f"(known kinds: {', '.join(registered_kinds()) or 'none registered'})"
            ),
        )
    return _REGISTRY[kind].verify(path)
