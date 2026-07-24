"""Property tests for artifact canonicalisation (issue #2608).

The artifact ``content_hash`` is the deterministic content-addressed identity a
non-coding task projects onto a signed lineage entry. These properties defend
the invariants that make that identity trustworthy:

* **Cross-run byte identity** - canonicalising the same input twice, from
  independently constructed values, yields byte-identical bytes (hence an
  identical ``content_hash``). This is the determinism heart of the contract.

* **Key-order independence** - a JSON object canonicalises to the same bytes
  regardless of the insertion order of its keys, so two operators who build the
  same object differently still converge.

* **Idempotent canonical form** - feeding already-canonical text back through
  the text canonicaliser is a fixed point (newlines already ``\\n``, already
  NFC), so re-canonicalisation never drifts.

Budgets are small so the file completes well under ~10 s on a hosted runner.
"""

from __future__ import annotations

import json
import unicodedata

from hypothesis import given, settings
from hypothesis import strategies as st

from bernstein.core.tasks.artifacts import (
    ArtifactKind,
    artifact_content_hash,
    canonicalise_artifact,
)

# JSON scalars that survive a canonical round-trip unchanged (no NaN/Inf; those
# are rejected by design; no floats, whose repr is interpreter-stable but noisy
# for this property).
_json_scalars = st.one_of(
    st.integers(min_value=-(10**9), max_value=10**9),
    st.booleans(),
    st.none(),
    st.text(max_size=40),
)
_json_objects = st.dictionaries(st.text(min_size=1, max_size=12), _json_scalars, max_size=6)


@settings(max_examples=150)
@given(st.lists(_json_objects, max_size=8))
def test_jsonl_cross_run_byte_identity(rows: list[dict[str, object]]) -> None:
    # Independently reconstruct the rows so no shared object identity leaks in.
    a = canonicalise_artifact(ArtifactKind.DATASET, rows)
    b = canonicalise_artifact(ArtifactKind.DATASET, [dict(r) for r in rows])
    assert a == b
    assert artifact_content_hash(ArtifactKind.DATASET, rows) == artifact_content_hash(
        ArtifactKind.DATASET, [dict(r) for r in rows]
    )


@settings(max_examples=150)
@given(_json_objects)
def test_ops_result_is_key_order_independent(obj: dict[str, object]) -> None:
    shuffled = dict(reversed(list(obj.items())))
    assert canonicalise_artifact(ArtifactKind.OPS_RESULT, obj) == canonicalise_artifact(
        ArtifactKind.OPS_RESULT, shuffled
    )


@settings(max_examples=150)
@given(st.text(max_size=200))
def test_text_canonical_form_is_a_fixed_point(text: str) -> None:
    # Restrict to NFC inputs; non-NFC is rejected by policy, not normalised.
    nfc = unicodedata.normalize("NFC", text)
    once = canonicalise_artifact(ArtifactKind.REPORT, nfc)
    # The canonical bytes decode to already-canonical text; re-canonicalising is
    # a fixed point (newlines already folded, already NFC).
    twice = canonicalise_artifact(ArtifactKind.REPORT, once.decode("utf-8"))
    assert once == twice


@settings(max_examples=100)
@given(st.lists(_json_objects, min_size=1, max_size=6))
def test_jsonl_lines_each_parse_back_to_a_row(rows: list[dict[str, object]]) -> None:
    canon = canonicalise_artifact(ArtifactKind.DATASET, rows)
    parsed = [json.loads(line) for line in canon.split(b"\n")]
    assert parsed == rows
