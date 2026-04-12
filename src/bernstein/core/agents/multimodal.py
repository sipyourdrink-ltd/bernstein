"""Multi-modal agent support for code, images, and diagrams.

Provides types and helpers for building multi-modal contexts that agents can
consume when processing tasks involving screenshots, architecture diagrams,
or other non-text artefacts.

Modality detection is extension-based. Base64 encoding uses the stdlib
``mimetypes`` module for MIME type resolution, falling back to
``application/octet-stream`` for unknown extensions.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Modality types
# ---------------------------------------------------------------------------


class ModalityType(StrEnum):
    """Supported input modality kinds."""

    TEXT = "text"
    IMAGE = "image"
    DIAGRAM = "diagram"
    AUDIO = "audio"


# ---------------------------------------------------------------------------
# Extension-to-modality mapping
# ---------------------------------------------------------------------------

_IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".webp",
        ".svg",
        ".tiff",
        ".ico",
    }
)

_DIAGRAM_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".mmd",
        ".dot",
        ".puml",
        ".drawio",
        ".d2",
    }
)

_AUDIO_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".mp3",
        ".wav",
        ".ogg",
        ".flac",
        ".m4a",
    }
)

# Text is the fallback for anything not matched above.

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MultiModalInput:
    """A single multi-modal input attached to a task context.

    Exactly one of ``content_path`` or ``content_base64`` should be set.

    Attributes:
        modality: The detected modality kind.
        content_path: Filesystem path to the original file, if available.
        content_base64: Base64-encoded content for inline transport.
        mime_type: MIME type string (e.g. ``image/png``).
        description: Human-readable description of the input.
    """

    modality: ModalityType
    content_path: Path | None = None
    content_base64: str | None = None
    mime_type: str = "application/octet-stream"
    description: str = ""


@dataclass(frozen=True)
class MultiModalContext:
    """Aggregated multi-modal context for a task.

    Attributes:
        inputs: Ordered collection of multi-modal inputs.
        primary_modality: The dominant modality across all inputs.
    """

    inputs: tuple[MultiModalInput, ...]
    primary_modality: ModalityType = ModalityType.TEXT


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------


def detect_modality(file_path: str | Path) -> ModalityType:
    """Detect modality from file extension.

    Args:
        file_path: Path to the file (only the extension is inspected).

    Returns:
        The detected ``ModalityType``.  Falls back to ``TEXT`` for
        unrecognised extensions.
    """
    suffix = Path(file_path).suffix.lower()
    if suffix in _IMAGE_EXTENSIONS:
        return ModalityType.IMAGE
    if suffix in _DIAGRAM_EXTENSIONS:
        return ModalityType.DIAGRAM
    if suffix in _AUDIO_EXTENSIONS:
        return ModalityType.AUDIO
    return ModalityType.TEXT


def encode_input(file_path: str | Path) -> MultiModalInput:
    """Base64-encode a file and return a ``MultiModalInput``.

    The MIME type is resolved via the stdlib ``mimetypes`` module.

    Args:
        file_path: Path to the file to encode.

    Returns:
        A ``MultiModalInput`` with ``content_base64`` populated.

    Raises:
        FileNotFoundError: If *file_path* does not exist.
    """
    path = Path(file_path)
    if not path.exists():
        msg = f"File not found: {path}"
        raise FileNotFoundError(msg)

    modality = detect_modality(path)
    mime_type, _ = mimetypes.guess_type(str(path))
    if mime_type is None:
        mime_type = "application/octet-stream"

    raw = path.read_bytes()
    encoded = base64.b64encode(raw).decode("ascii")

    return MultiModalInput(
        modality=modality,
        content_path=path,
        content_base64=encoded,
        mime_type=mime_type,
        description=path.name,
    )


def build_multimodal_context(files: list[str | Path]) -> MultiModalContext:
    """Build a ``MultiModalContext`` from a list of file paths.

    Each file is encoded and its modality detected. The primary modality is
    determined by simple majority vote; ties are broken by the ordering of
    ``ModalityType`` members (TEXT < IMAGE < DIAGRAM < AUDIO).

    Args:
        files: Paths to include in the context.

    Returns:
        A ``MultiModalContext`` aggregating all inputs.
    """
    inputs: list[MultiModalInput] = []
    for fp in files:
        try:
            inputs.append(encode_input(fp))
        except FileNotFoundError:
            logger.warning("Skipping missing file: %s", fp)

    if not inputs:
        return MultiModalContext(inputs=(), primary_modality=ModalityType.TEXT)

    # Majority-vote for primary modality.
    counts: dict[ModalityType, int] = {}
    for inp in inputs:
        counts[inp.modality] = counts.get(inp.modality, 0) + 1

    max_count = max(counts.values())
    # Among tied modalities, pick the first in ModalityType declaration order.
    primary = ModalityType.TEXT
    for m in ModalityType:
        if counts.get(m, 0) == max_count:
            primary = m
            break

    return MultiModalContext(inputs=tuple(inputs), primary_modality=primary)


# ---------------------------------------------------------------------------
# Adapter capability check
# ---------------------------------------------------------------------------

_MULTIMODAL_ADAPTERS: frozenset[str] = frozenset(
    {
        "claude",
        "gemini",
    }
)


def is_multimodal_capable(adapter_name: str) -> bool:
    """Check whether an adapter supports multi-modal inputs.

    Args:
        adapter_name: The adapter name as used in the adapter registry
            (e.g. ``"claude"``, ``"gemini"``, ``"codex"``).

    Returns:
        ``True`` if the adapter is known to accept non-text inputs.
    """
    return adapter_name.lower() in _MULTIMODAL_ADAPTERS
