"""Old lineage records keep verifying after the URI namespace (issue #2559).

Every lineage record written before this change is keyed by a bare
repo-relative path. The chosen compatibility strategy is **implicit scheme, no
migration**: a bare path is the canonical on-wire form of the ``repo`` scheme,
so nothing on disk is rewritten, no dual-read path exists, and the boundary's
accept / reject verdict for a repo path is unchanged.

The golden values below were produced by the pre-#2559 code on ``origin/main``
and pasted verbatim. If widening the key space had perturbed the entry hash,
the HMAC tag, the canonical row bytes or the accept set, these literals would
break -- which is exactly the alarm they exist to raise.
"""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.core.lineage.entry import LineageEntry, compute_operator_hmac, entry_hash
from bernstein.core.lineage.spine import LineageSpine, SpineStatus

# --- Goldens captured from the pre-#2559 implementation ---------------------

_GOLDEN_HMAC_KEY = b"golden-key"
_GOLDEN_RUN_ID = "golden-run"

_GOLDEN_ROW_1 = (
    '{"actor":"agent:worker-1","artifact_path":"src/bernstein/core/lineage/spine.py",'
    '"content_hash":"sha256:8ed3f6ad685b959ead7022518e1af76cd816f8e8ec7ccdda1ed4018e8f2223f8",'
    '"entry_hash":"sha256:8448464d8be39051a7771574bb5af58d6e7ec37f8bc166c5529f3db2dc724022",'
    '"hmac":"e65e17aa41d0a952ebdffd3cfe7aa8464f56843c68ad27de44c71e146a3ec451",'
    '"model":"sonnet","prev_hash":"","step_id":"task-1","timestamp":1700000000000000000,"v":1}'
)
_GOLDEN_ROW_2 = (
    '{"actor":"agent:worker-2","artifact_path":"docs/lineage.md",'
    '"content_hash":"sha256:f44e64e75f3948e9f73f8dfa94721c4ce8cbb4f265c4790c702b2d41cfbf2753",'
    '"entry_hash":"sha256:a7d45b063325f0538e2a62e98cdc3dec1f42a18a10e472b0794529ab9e94e3f6",'
    '"hmac":"34472b3a671380774727795640cfc9499f88f82c926d9fc09a4a17fc1648f704",'
    '"model":"opus","prev_hash":"sha256:8448464d8be39051a7771574bb5af58d6e7ec37f8bc166c5529f3db2dc724022",'
    '"step_id":"task-2","timestamp":1700000001000000000,"v":1}'
)

_GOLDEN_ENTRY_HASH_1 = "sha256:8448464d8be39051a7771574bb5af58d6e7ec37f8bc166c5529f3db2dc724022"
_GOLDEN_ENTRY_HASH_2 = "sha256:a7d45b063325f0538e2a62e98cdc3dec1f42a18a10e472b0794529ab9e94e3f6"

_GOLDEN_V1_ENTRY_HASH = "sha256:0736d77f457187f1cf2be2b8e0b05123ddec3b298a7802279ae6e5973bc926d1"
_GOLDEN_V1_HMAC = "c66884314da2b102d5307f33bb4cbb9d1fcbc914b95cea82a9100b65fbec02d1"


def _write_golden_spine(root: Path) -> LineageSpine:
    run_dir = root / _GOLDEN_RUN_ID
    run_dir.mkdir(parents=True)
    (run_dir / "spine.jsonl").write_text(f"{_GOLDEN_ROW_1}\n{_GOLDEN_ROW_2}\n", encoding="utf-8")
    return LineageSpine(root, run_id=_GOLDEN_RUN_ID, hmac_key=_GOLDEN_HMAC_KEY)


def test_pre_feature_spine_still_verifies(tmp_path: Path) -> None:
    """A chain written before the URI namespace verifies untouched."""
    spine = _write_golden_spine(tmp_path)
    result = spine.verify()
    assert result.ok, result
    assert result.status is SpineStatus.OK
    assert spine.head_hash() == _GOLDEN_ENTRY_HASH_2


def test_recording_the_same_inputs_reproduces_the_golden_hashes(tmp_path: Path) -> None:
    """Replaying the pre-feature writes today produces byte-identical rows.

    This is the strongest form of the compatibility claim: not merely that old
    records still verify, but that the writer produces the same bytes for the
    same inputs, so an old and a new record of the same write are the same
    record.
    """
    spine = LineageSpine(tmp_path, run_id=_GOLDEN_RUN_ID, hmac_key=_GOLDEN_HMAC_KEY)
    first = spine.record(
        artifact_path="src/bernstein/core/lineage/spine.py",
        content=b"alpha",
        actor="agent:worker-1",
        step_id="task-1",
        model="sonnet",
        timestamp=1700000000000000000,
    )
    second = spine.record(
        artifact_path="docs/lineage.md",
        content=b"beta",
        actor="agent:worker-2",
        step_id="task-2",
        model="opus",
        timestamp=1700000001000000000,
    )
    assert first == _GOLDEN_ENTRY_HASH_1
    assert second == _GOLDEN_ENTRY_HASH_2

    rows = (tmp_path / _GOLDEN_RUN_ID / "spine.jsonl").read_text(encoding="utf-8").splitlines()
    assert rows == [_GOLDEN_ROW_1, _GOLDEN_ROW_2]


def test_pre_feature_spine_still_detects_tampering(tmp_path: Path) -> None:
    """Compatibility must not have bought itself a weaker check."""
    spine = _write_golden_spine(tmp_path)
    path = tmp_path / _GOLDEN_RUN_ID / "spine.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows[0]["artifact_path"] = "src/bernstein/core/lineage/spine.py.evil"
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False, separators=(",", ":"), sort_keys=True) for r in rows) + "\n",
        encoding="utf-8",
    )
    assert not spine.verify().ok


def test_pre_feature_v1_entry_hash_and_hmac_are_unchanged() -> None:
    """The v1 signed-entry wire form is untouched by the widened key space."""
    entry = LineageEntry(
        v=1,
        artefact_path="src/a/b.py",
        artefact_kind="file",
        content_hash="sha256:" + "ab" * 32,
        parent_hashes=[],
        agent_id="agent:w",
        agent_card_kid="kid-1",
        tool_call_id="tc-1",
        span_id="span-1",
        ts_ns=1700000000000000000,
        operator_hmac="",
    )
    assert entry_hash(entry) == _GOLDEN_V1_ENTRY_HASH
    assert compute_operator_hmac(entry, _GOLDEN_HMAC_KEY) == _GOLDEN_V1_HMAC
