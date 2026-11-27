"""Memorable deterministic run names for user-facing surfaces (#1626).

Run ids stay UUIDs internally. This module renders a deterministic,
memorable ``<adjective>-<noun>-<NN>`` label from a UUID so operators can
read, type, and remember a run without copying a seven-character hex
prefix between windows. The mapping is pure: the same UUID always renders
to the same name, so logs, dashboards, and status panels stay consistent.

Rendering recipe (documented and stable across versions):

1. Take the UUID's 128-bit integer value (``UUID.int``).
2. Derive a digest with ``blake2b(uuid.bytes, digest_size=8)`` and read it
   as a big-endian unsigned integer ``h``. Using a hash (rather than the
   raw int) spreads adjacent UUIDs across the word space so sequential or
   structured ids do not cluster onto neighbouring names.
3. ``adjective = ADJECTIVES[h % len(ADJECTIVES)]``.
4. ``noun = NOUNS[(h // len(ADJECTIVES)) % len(NOUNS)]``.
5. ``suffix = (h // (len(ADJECTIVES) * len(NOUNS))) % 100`` rendered as a
   zero-padded two-digit number.

The word lists are fixed, English-only, short (max 8 chars), unambiguous,
and non-product-specific. The name space is
``len(ADJECTIVES) * len(NOUNS) * 100`` distinct names; see
:func:`name_space_size`. ``render_name`` is *not* globally bijective
across all UUIDs (128 bits cannot fit in the name space), so callers that
need a reverse lookup must track the names they have actually rendered;
:func:`find_collisions` reports when two known UUIDs collide.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = [
    "ADJECTIVES",
    "MAX_WORD_LEN",
    "NAME_RE",
    "NOUNS",
    "build_lookup",
    "find_collisions",
    "is_run_name",
    "name_space_size",
    "render_name",
]

# ---------------------------------------------------------------------------
# Word lists
# ---------------------------------------------------------------------------
# Neutral, English-only, lowercase, max 8 characters, no product or themed
# names. Kept sorted and de-duplicated; both lists have a power-of-friendly
# length only by coincidence -- the recipe does not assume any size.

ADJECTIVES: tuple[str, ...] = (
    "amber",
    "ample",
    "arctic",
    "brave",
    "brisk",
    "calm",
    "clear",
    "clever",
    "cosmic",
    "crisp",
    "daring",
    "dawn",
    "deep",
    "eager",
    "early",
    "fancy",
    "fleet",
    "fond",
    "gentle",
    "glad",
    "golden",
    "grand",
    "happy",
    "humble",
    "ideal",
    "jolly",
    "keen",
    "kind",
    "lively",
    "loyal",
    "lucid",
    "merry",
    "mild",
    "neat",
    "noble",
    "polite",
    "prime",
    "proud",
    "quick",
    "quiet",
    "rapid",
    "ready",
    "royal",
    "sharp",
    "shiny",
    "silent",
    "smart",
    "smooth",
    "snug",
    "solid",
    "spry",
    "steady",
    "sunny",
    "swift",
    "tidy",
    "trim",
    "true",
    "trusty",
    "vivid",
    "warm",
    "witty",
    "young",
    "zesty",
    "zippy",
)

NOUNS: tuple[str, ...] = (
    "acorn",
    "anchor",
    "arbor",
    "badger",
    "basin",
    "beacon",
    "birch",
    "bison",
    "bramble",
    "brook",
    "canyon",
    "cedar",
    "cliff",
    "comet",
    "coral",
    "cove",
    "crane",
    "delta",
    "dune",
    "eagle",
    "ember",
    "fern",
    "field",
    "finch",
    "fjord",
    "forest",
    "garnet",
    "geyser",
    "glacier",
    "grove",
    "harbor",
    "heron",
    "island",
    "ivy",
    "jasper",
    "kelp",
    "lagoon",
    "lake",
    "lark",
    "leaf",
    "ledge",
    "lichen",
    "lotus",
    "maple",
    "marsh",
    "meadow",
    "mesa",
    "moss",
    "oasis",
    "otter",
    "pebble",
    "petal",
    "pine",
    "plateau",
    "pond",
    "quail",
    "quartz",
    "raven",
    "reef",
    "ridge",
    "river",
    "robin",
    "sable",
    "shore",
    "slate",
    "sparrow",
    "spruce",
    "summit",
    "tarn",
    "thicket",
    "tundra",
    "valley",
    "willow",
    "wren",
)

MAX_WORD_LEN = 8

# A rendered name is ``<word>-<word>-NN``. The suffix is always two digits.
NAME_RE = re.compile(r"^[a-z]+-[a-z]+-\d{2}$")

# Number of distinct two-digit suffixes (00..99).
_SUFFIX_MODULO = 100


def name_space_size() -> int:
    """Return the count of distinct names ``render_name`` can produce."""
    return len(ADJECTIVES) * len(NOUNS) * _SUFFIX_MODULO


def _digest_int(run_id: UUID) -> int:
    """Return a stable 64-bit integer digest of *run_id*.

    Uses BLAKE2b over the UUID's raw 16 bytes so the mapping does not
    depend on the platform hash seed and stays identical across Python
    versions and runs.
    """
    digest = hashlib.blake2b(run_id.bytes, digest_size=8).digest()
    return int.from_bytes(digest, byteorder="big", signed=False)


def render_name(run_id: UUID) -> str:
    """Render a deterministic ``<adjective>-<noun>-<NN>`` name for *run_id*.

    Args:
        run_id: The internal run UUID.

    Returns:
        A lowercase memorable name, for example ``"swift-otter-07"``.

    Raises:
        TypeError: If *run_id* is not a :class:`uuid.UUID`.
    """
    # Defensive runtime guard: callers feeding values parsed from JSON or
    # untyped dicts may pass a str; the annotation alone does not protect us.
    if not isinstance(run_id, UUID):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise TypeError(f"render_name expects a UUID, got {type(run_id).__name__}")

    h = _digest_int(run_id)
    adj_count = len(ADJECTIVES)
    noun_count = len(NOUNS)

    adjective = ADJECTIVES[h % adj_count]
    noun = NOUNS[(h // adj_count) % noun_count]
    suffix = (h // (adj_count * noun_count)) % _SUFFIX_MODULO
    return f"{adjective}-{noun}-{suffix:02d}"


def is_run_name(value: str) -> bool:
    """Return ``True`` when *value* has the rendered run-name shape.

    This is a shape check only; it does not confirm that any UUID renders
    to *value*. Use :func:`build_lookup` for a verified reverse mapping.
    """
    return bool(NAME_RE.match(value))


def build_lookup(run_ids: Iterable[UUID]) -> dict[str, UUID]:
    """Build a name -> UUID lookup for a known set of run ids.

    When two ids render to the same name, the first id encountered wins.
    Use :func:`find_collisions` to detect such cases before relying on the
    lookup.

    Args:
        run_ids: The run ids whose names should be resolvable.

    Returns:
        Mapping from rendered name to the first UUID that produced it.
    """
    lookup: dict[str, UUID] = {}
    for run_id in run_ids:
        name = render_name(run_id)
        lookup.setdefault(name, run_id)
    return lookup


def find_collisions(run_ids: Iterable[UUID]) -> dict[str, list[UUID]]:
    """Return names produced by more than one of the given *run_ids*.

    Args:
        run_ids: The run ids to check.

    Returns:
        Mapping from a colliding name to its distinct UUIDs in first-seen
        order. Empty when no collisions exist.
    """
    grouped: dict[str, list[UUID]] = defaultdict(list)
    for run_id in dict.fromkeys(run_ids):
        grouped[render_name(run_id)].append(run_id)
    return {name: ids for name, ids in grouped.items() if len(ids) > 1}
