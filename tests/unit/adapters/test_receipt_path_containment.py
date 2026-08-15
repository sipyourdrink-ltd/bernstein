"""Containment tests for the three adapter receipt-write sites (issue #3821).

All three sites (`security_floor`, `canary`, `admission`) sealed a receipt by
writing a ``.json.tmp`` sibling and renaming it over the content-addressed
name. The containment check covered the *final* name only, so the temporary
path -- the one actually opened first -- was never proven to be inside the
receipts directory.

Each site gets an escape test paired with a positive control, so an
unconditional refusal cannot satisfy the suite.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

import pytest

from bernstein.adapters.admission import write_admission_receipt
from bernstein.adapters.canary import write_canary_receipt
from bernstein.adapters.security_floor import receipt_sha256, write_preflight_receipt


class _ReceiptWriter(Protocol):
    def __call__(self, base_dir: Path, receipt: dict[str, Any]) -> Path: ...


#: The three converted sites. Byte-identical code before this change.
WRITERS: list[tuple[str, _ReceiptWriter]] = [
    ("security_floor", write_preflight_receipt),
    ("canary", write_canary_receipt),
    ("admission", write_admission_receipt),
]


def _receipt(adapter: str = "claude") -> dict[str, Any]:
    return {"adapter": adapter, "kind": "spawn_preflight", "verdict": "admit"}


def _content_addressed_stem(receipt: dict[str, Any]) -> str:
    """Return the ``<adapter>-<sha16>`` stem the writers derive."""
    return f"{receipt['adapter']}-{receipt_sha256(receipt)[:16]}"


@pytest.mark.parametrize(("site", "write"), WRITERS, ids=[name for name, _ in WRITERS])
def test_receipt_write_refuses_symlinked_temp_path(site: str, write: _ReceiptWriter, tmp_path: Path) -> None:
    """A pre-placed ``.json.tmp`` symlink must not capture the receipt write.

    Before this change the write followed the link and overwrote a file
    outside the receipts directory, then renamed the link into place -- so the
    "sealed" receipt was a symlink and an arbitrary host file held the receipt
    body.
    """
    base = tmp_path / "receipts"
    base.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim.txt"
    victim.write_text("ORIGINAL\n", encoding="utf-8")

    receipt = _receipt()
    (base / f"{_content_addressed_stem(receipt)}.json.tmp").symlink_to(victim)

    with pytest.raises(ValueError, match="escapes the receipts directory"):
        write(base, receipt)

    assert victim.read_text(encoding="utf-8") == "ORIGINAL\n"


@pytest.mark.parametrize(("site", "write"), WRITERS, ids=[name for name, _ in WRITERS])
def test_receipt_write_refuses_symlinked_final_path(site: str, write: _ReceiptWriter, tmp_path: Path) -> None:
    """The pre-existing refusal on the final name survives the conversion."""
    base = tmp_path / "receipts"
    base.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim.json"
    victim.write_text("ORIGINAL\n", encoding="utf-8")

    receipt = _receipt()
    (base / f"{_content_addressed_stem(receipt)}.json").symlink_to(victim)

    with pytest.raises(ValueError, match="escapes the receipts directory"):
        write(base, receipt)

    assert victim.read_text(encoding="utf-8") == "ORIGINAL\n"


@pytest.mark.parametrize(("site", "write"), WRITERS, ids=[name for name, _ in WRITERS])
def test_receipt_write_seals_an_ordinary_receipt(site: str, write: _ReceiptWriter, tmp_path: Path) -> None:
    """Positive control: an ordinary write still lands under the base."""
    base = tmp_path / "receipts"
    receipt = _receipt()

    path = write(base, receipt)

    assert path.is_file()
    assert not path.is_symlink()
    assert path.parent.resolve() == base.resolve()
    # The temporary sibling is cleaned up by the rename.
    assert not (base / f"{_content_addressed_stem(receipt)}.json.tmp").exists()


@pytest.mark.parametrize(("site", "write"), WRITERS, ids=[name for name, _ in WRITERS])
def test_receipt_write_still_refuses_a_hostile_adapter_name(site: str, write: _ReceiptWriter, tmp_path: Path) -> None:
    """Positive control for the allow-pattern: traversal never reaches the join."""
    base = tmp_path / "receipts"
    base.mkdir()

    with pytest.raises(ValueError, match="invalid adapter name"):
        write(base, _receipt(adapter="../../etc/passwd"))


@pytest.mark.parametrize(("site", "write"), WRITERS, ids=[name for name, _ in WRITERS])
def test_receipt_write_refuses_an_over_long_adapter_name(site: str, write: _ReceiptWriter, tmp_path: Path) -> None:
    """An adapter name too long to name a file is a ValueError, not an OSError.

    The allow-pattern bounds the alphabet but not the length, so a long name
    previously reached ``open()`` and raised ``OSError(ENAMETOOLONG)`` --
    outside the ``ValueError`` the callers guard on.
    """
    base = tmp_path / "receipts"
    base.mkdir()

    with pytest.raises(ValueError):
        write(base, _receipt(adapter="a" * 300))
