"""Scanner registry - look up scanner adapters by name.

Mirrors the CLI adapter registry pattern but for external analysis tools
that implement the ScannerAdapter contract instead of spawning CLI processes.
"""

from __future__ import annotations

import inspect
import logging
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from bernstein.adapters.gitleaks import GitleaksAdapter
from bernstein.adapters.scanner import ScannerAdapter

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)


@runtime_checkable
class ScannerAdmissionGateLike(Protocol):
    """Structural type for scanner admission checks (placeholder for future)."""

    def admit(self, scanner: str) -> object | None:
        """Admit a scanner if it passes some gate check."""
        ...


_SCANNERS: dict[str, type[ScannerAdapter] | ScannerAdapter] = {}

#: Registry names that no longer resolve to a scanner, mapped to guidance.
_REMOVED_SCANNERS: dict[str, str] = {}

_entrypoints_loaded = False


def _load_entrypoint_scanners() -> None:
    """Discover and register scanners from the ``bernstein.scanners`` entry-point group."""
    global _entrypoints_loaded
    if _entrypoints_loaded:
        return
    _entrypoints_loaded = True
    for ep in entry_points(group="bernstein.scanners"):
        try:
            loaded = ep.load()
            name = ep.name
            if (inspect.isclass(loaded) and issubclass(loaded, ScannerAdapter)) or isinstance(loaded, ScannerAdapter):
                _SCANNERS[name] = loaded
            else:
                logger.warning(
                    "Ignoring entry-point scanner %r: expected ScannerAdapter subclass or instance, got %r",
                    name,
                    loaded,
                )
        except Exception as exc:  # pragma: no cover
            logger.warning("Failed to load entry-point scanner %r: %s", ep.name, exc)


def get_scanner(scanner_name: str, *, admission_gate: ScannerAdmissionGateLike | None = None) -> ScannerAdapter:
    """Get scanner by name, e.g. 'grype', 'bandit', 'semgrep'.

    Resolution is name-based by default. Passing ``admission_gate`` makes resolution
    proof-based instead (future extension).

    Args:
        scanner_name: Scanner name to look up.
        admission_gate: Optional gate for proof-based resolution.

    Returns:
        An instantiated ScannerAdapter.

    Raises:
        ValueError: If the scanner name is not recognized or names a removed scanner.
    """
    _load_entrypoint_scanners()

    scanner_cls = _SCANNERS.get(scanner_name)
    if scanner_cls is None:
        removed = removed_scanner_message(scanner_name)
        if removed is not None:
            raise ValueError(removed)
        available = ", ".join(sorted([*_SCANNERS.keys()]))
        raise ValueError(f"Unknown scanner '{scanner_name}'. Available: {available}")

    if admission_gate is not None:
        admission_gate.admit(scanner_name)

    if isinstance(scanner_cls, ScannerAdapter):
        return scanner_cls
    return scanner_cls()


def removed_scanner_message(scanner_name: str) -> str | None:
    """Return replacement guidance for a removed scanner name."""
    return _REMOVED_SCANNERS.get(scanner_name)


def registry_name_for(scanner: ScannerAdapter) -> str | None:
    """Return the registry key a scanner instance is registered under."""
    explicit = getattr(scanner, "registry_name", "") or ""
    if explicit and explicit in _SCANNERS:
        return explicit

    _load_entrypoint_scanners()
    scanner_type = type(scanner)
    for name, entry in _SCANNERS.items():
        if entry is scanner:
            return name
        if inspect.isclass(entry) and entry is scanner_type:
            return name
    return None


def register_scanner(name: str, scanner: type[ScannerAdapter] | ScannerAdapter) -> None:
    """Register a custom scanner by name."""
    _SCANNERS[name] = scanner


def iter_scanner_specs() -> Iterator[tuple[str, type[ScannerAdapter] | ScannerAdapter]]:
    """Yield every registered scanner as ``(name, class-or-instance)`` pairs."""
    _load_entrypoint_scanners()
    for name in sorted(_SCANNERS.keys()):
        yield name, _SCANNERS[name]


_NON_ANALYZER_STUBS: frozenset[str] = frozenset()  # Placeholder for test-only scanners


def selectable_scanner_names() -> frozenset[str]:
    """Return the scanner registry names an operator may select for a scan."""
    return frozenset(name for name, _ in iter_scanner_specs() if name not in _NON_ANALYZER_STUBS)


def _registered_scanner_name(name: str) -> str | None:
    """Return *name* if it is a scanner registry key, else ``None``."""
    _load_entrypoint_scanners()
    if name in _SCANNERS:
        return name
    return None


def scanner_name_for_provider(provider_name: str | None, model: str) -> str | None:
    """Resolve a scanner registry name from a provider name and/or model string.

    Placeholder for future provider-to-scanner mapping (not needed for slice 2).
    """
    # Not implemented in slice 2 - external tools are looked up by name directly
    return None


register_scanner(GitleaksAdapter.registry_name, GitleaksAdapter)
