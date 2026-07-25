"""Golden: the signed-lineage write path stays byte-identical to the v1 recorder.

Issue #2960 moved the sealing body out of the deprecated
:mod:`bernstein.core.lineage.recorder` into the supported
:mod:`bernstein.core.lineage.signed_write`. The security model is the on-disk
bytes, so "byte-identical" is the acceptance criterion, not a nice-to-have: a
lineage entry whose canonical pre-image shifted by one byte invalidates every
signature and every operator HMAC previously written against it, and every
receipt anchored on one.

The fixtures below were captured by running the *pre-migration*
``LineageRecorder.record_write`` against pinned inputs, and are pinned verbatim.
Every byte the store lays down has to reproduce: the JCS log line, the operator
HMAC inside it, the Ed25519 detached-JWS sidecar, the tip projection and the
per-artefact projection.

Ed25519 (RFC 8032) signs deterministically, so with a pinned key, pinned inputs
and a pinned ``ts_ns`` the signature bytes are themselves a golden value - a
drift in the canonical pre-image surfaces as a different JWS rather than as a
flake.

The four pinned writes cover both migrated receipt shapes plus the chaining
behaviour they depend on:

* a genesis ``query-result`` entry and its linear successor on the same artefact
  (the datasource query-receipt shape, ``span_id`` carrying a binding digest),
* an ``sdd-runtime`` entry with the all-zero span (the payment-receipt shape),
* a ``tool-result`` entry with a ``trust_class`` and a cross-artefact
  ``extra_parents`` edge.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from bernstein.core.datasources.connection import DataSourceConnection
from bernstein.core.datasources.receipt import QueryReceiptStore
from bernstein.core.lineage import signed_write as signed_write_module
from bernstein.core.lineage.entry import canonicalise, entry_hash
from bernstein.core.lineage.identity import AgentCard, verify_detached
from bernstein.core.lineage.signed_write import SignedLineageLog, seal_write
from bernstein.core.lineage.store import LineageStore

if TYPE_CHECKING:
    from collections.abc import Iterator

# --- pinned identity --------------------------------------------------------

_PRIVATE_PEM = "-----BEGIN PRIVATE KEY-----\nMC4CAQAwBQYDK2VwBCIEIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4f\n-----END PRIVATE KEY-----\n"

_PUBLIC_PEM = "-----BEGIN PUBLIC KEY-----\nMCowBQYDK2VwAyEAA6EHv/POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg=\n-----END PUBLIC KEY-----\n"

_CARD = AgentCard(agent_id="agent:golden", kid="golden-kid-1", public_key_pem=_PUBLIC_PEM)
_AGENT_ID = "agent:golden"
_OPERATOR_KEY = b"golden-operator-hmac-key-32bytes"
_FIXED_NS = 1_700_000_000_000_000_001

# --- pinned writes ----------------------------------------------------------

_WRITES: list[dict[str, Any]] = [
    {
        "artefact_path": ".sdd/datasources/queries/sales/aaaa_bbbb.jsonl",
        "new_content": b'{"rows":[[1,"a"]]}',
        "tool_call_id": "query:sales",
        "span_id": "b" * 64,
        "artefact_kind": "query-result",
        "trust_class": None,
        "extra_parents": None,
        "ts_ns": 1_700_000_000_000_000_001,
    },
    {
        "artefact_path": ".sdd/datasources/queries/sales/aaaa_bbbb.jsonl",
        "new_content": b'{"rows":[[1,"a"],[2,"b"]]}',
        "tool_call_id": "query:sales",
        "span_id": "c" * 64,
        "artefact_kind": "query-result",
        "trust_class": None,
        "extra_parents": None,
        "ts_ns": 1_700_000_000_000_000_002,
    },
    {
        "artefact_path": ".sdd/payments/receipts/deadbeef.json",
        "new_content": b'{"decision":"authorized"}',
        "tool_call_id": "sha256:deadbeef",
        "span_id": "0" * 16,
        "artefact_kind": "sdd-runtime",
        "trust_class": None,
        "extra_parents": None,
        "ts_ns": 1_700_000_000_000_000_003,
    },
    {
        "artefact_path": "provenance/web-fetch/0123456789abcdef",
        "new_content": b"tool result bytes",
        "tool_call_id": "tc-1",
        "span_id": "d" * 16,
        "artefact_kind": "tool-result",
        "trust_class": "third_party",
        "extra_parents": ["sha256:" + "f" * 64],
        "ts_ns": 1_700_000_000_000_000_004,
    },
]

# --- golden values captured from the pre-migration recorder -----------------

_GOLDEN_ENTRY_HASHES = [
    "sha256:deb1afd74f366ab843b81bc3870f11abb66dffa7517fdd52971c7abc52b3971e",
    "sha256:595bbbf0912273eb1d888d439b8f5fa506303678aedaddfcebdf81cd4e241fa9",
    "sha256:2e0da03cb6c7b885bf42f009d8a7e29bdb1557df6f981be01b91d5fad718b254",
    "sha256:6a27540503ba199db1228c8fc6de5f8ac963ae0502bb7c68ade41628945fe86e",
]

_GOLDEN_LOG_JSONL = '{"agent_card_kid":"golden-kid-1","agent_id":"agent:golden","artefact_kind":"query-result","artefact_path":".sdd/datasources/queries/sales/aaaa_bbbb.jsonl","content_hash":"sha256:8fcdff05f0875521ab5e9922ba713607eaffd79d564d7a62cd570d1d895d6a43","operator_hmac":"5ffd67f70fba4f1fc6766ce6ff3dd64a79ed63bde0f34c7c50eae5fd7eb0447a","parent_hashes":[],"span_id":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","tool_call_id":"query:sales","ts_ns":1700000000000000001,"v":1}\n{"agent_card_kid":"golden-kid-1","agent_id":"agent:golden","artefact_kind":"query-result","artefact_path":".sdd/datasources/queries/sales/aaaa_bbbb.jsonl","content_hash":"sha256:66bc805454af11bd3a1a45563821429f23e12827be151676d1a3bcc6775be99b","operator_hmac":"0befc5bd3fd13a2967c923720f1ff771f13fa22d062b9662093d412811c891fe","parent_hashes":["sha256:deb1afd74f366ab843b81bc3870f11abb66dffa7517fdd52971c7abc52b3971e"],"span_id":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc","tool_call_id":"query:sales","ts_ns":1700000000000000002,"v":1}\n{"agent_card_kid":"golden-kid-1","agent_id":"agent:golden","artefact_kind":"sdd-runtime","artefact_path":".sdd/payments/receipts/deadbeef.json","content_hash":"sha256:c29c0017f2b13d5a5eac15adcd8e276c2653aadde40c581a2a95f1e38aa97eaa","operator_hmac":"30dd2d6b55ae336eb84f007cf5e652b0ca45609557a05223f7f9c017a6bff1b7","parent_hashes":[],"span_id":"0000000000000000","tool_call_id":"sha256:deadbeef","ts_ns":1700000000000000003,"v":1}\n{"agent_card_kid":"golden-kid-1","agent_id":"agent:golden","artefact_kind":"tool-result","artefact_path":"provenance/web-fetch/0123456789abcdef","content_hash":"sha256:2bea7a6b2569e1b8b64ee6ef1a6d5fd621f9175083d6508dc5de6511cb7b7bcc","operator_hmac":"28ad1d19bc2119553f7bf8c41b60e82d6c64ed88146cda395639b8579405dd37","parent_hashes":["sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"],"span_id":"dddddddddddddddd","tool_call_id":"tc-1","trust_class":"third_party","ts_ns":1700000000000000004,"v":1}\n'

_GOLDEN_JWS = {
    "signatures/32/32d9ad089a5f11bea1928e8d74e35d1a69f884506e5d4140470ab0bc7e6c7a12/6a27540503ba199db1228c8fc6de5f8ac963ae0502bb7c68ade41628945fe86e.jws": "eyJhbGciOiJFZERTQSIsImI2NCI6ZmFsc2UsImNyaXQiOlsiYjY0Il0sImtpZCI6ImdvbGRlbi1raWQtMSJ9..S7CromjmAur3vBbYR697oKJZNruUqCZjsxRZDtwIxzlo5mLhSo66EcyxQXtZUuJncisr0-NDsKG-aTWDFhSjAw",
    "signatures/7d/7d7cb30784fd17b481e0ebd6fef60c45d92cef2da5bb7323b6f97e55a47710f8/595bbbf0912273eb1d888d439b8f5fa506303678aedaddfcebdf81cd4e241fa9.jws": "eyJhbGciOiJFZERTQSIsImI2NCI6ZmFsc2UsImNyaXQiOlsiYjY0Il0sImtpZCI6ImdvbGRlbi1raWQtMSJ9..E-PLT3H5j28zkRnuZCQNtEc86pXxKCbYrgqmEFgROjY9yxyeCSd6_dh7GojprHJtpFO0G2f2HVExPP5Kcr3lCw",
    "signatures/7d/7d7cb30784fd17b481e0ebd6fef60c45d92cef2da5bb7323b6f97e55a47710f8/deb1afd74f366ab843b81bc3870f11abb66dffa7517fdd52971c7abc52b3971e.jws": "eyJhbGciOiJFZERTQSIsImI2NCI6ZmFsc2UsImNyaXQiOlsiYjY0Il0sImtpZCI6ImdvbGRlbi1raWQtMSJ9..uEG-KnHSQ-XJ6qpi-cULU6DAdDkIXSwhOdIxUxzWmmIXkiDOmW-PlqIQTZUXQ-9Gk-Irigk2tRfOwbRH5kxgBQ",
    "signatures/90/909fce934e0dbeca05f7cac876d38c25fdd6222357642bc310f4c54226b7a073/2e0da03cb6c7b885bf42f009d8a7e29bdb1557df6f981be01b91d5fad718b254.jws": "eyJhbGciOiJFZERTQSIsImI2NCI6ZmFsc2UsImNyaXQiOlsiYjY0Il0sImtpZCI6ImdvbGRlbi1raWQtMSJ9..berhflXYhJh-6eiXL-cmBWCUDjF1YUV12z1xH9SDuFonLCsE4zFkvhyyIrsfCalBCXM4McTTukFb68x1o_mdDw",
}

_GOLDEN_TIPS = {
    "tips/32d9ad089a5f11bea1928e8d74e35d1a69f884506e5d4140470ab0bc7e6c7a12.json": '{"merged":[],"open":["sha256:6a27540503ba199db1228c8fc6de5f8ac963ae0502bb7c68ade41628945fe86e"]}',
    "tips/7d7cb30784fd17b481e0ebd6fef60c45d92cef2da5bb7323b6f97e55a47710f8.json": '{"merged":[],"open":["sha256:595bbbf0912273eb1d888d439b8f5fa506303678aedaddfcebdf81cd4e241fa9"]}',
    "tips/909fce934e0dbeca05f7cac876d38c25fdd6222357642bc310f4c54226b7a073.json": '{"merged":[],"open":["sha256:2e0da03cb6c7b885bf42f009d8a7e29bdb1557df6f981be01b91d5fad718b254"]}',
}

_GOLDEN_TREE_SHA256 = {
    "by-artefact/32/32d9ad089a5f11bea1928e8d74e35d1a69f884506e5d4140470ab0bc7e6c7a12.jsonl": "2f038cad5dcbc30e843855ec3f65317ce8013fc13f58413e3422ac4b6c2c763a",
    "by-artefact/7d/7d7cb30784fd17b481e0ebd6fef60c45d92cef2da5bb7323b6f97e55a47710f8.jsonl": "48f9d0d5e69d3846f921107fe05fdac5be372410207a75406ffcd7036672b528",
    "by-artefact/90/909fce934e0dbeca05f7cac876d38c25fdd6222357642bc310f4c54226b7a073.jsonl": "42dd473198cb5a8eb2db323f36cf0ce74882ab07abfddac629852cd0cc39c672",
    "log.jsonl": "73a6c2fd202fa6a203e7ac95e7988a218de40fe9b81f874a63a897c1a79b1763",
    "signatures/32/32d9ad089a5f11bea1928e8d74e35d1a69f884506e5d4140470ab0bc7e6c7a12/6a27540503ba199db1228c8fc6de5f8ac963ae0502bb7c68ade41628945fe86e.jws": "a74c5b9957fb04ac84b4e2f3d23891b8bd9de6abc7dd080959ea8f084bd4e655",
    "signatures/7d/7d7cb30784fd17b481e0ebd6fef60c45d92cef2da5bb7323b6f97e55a47710f8/595bbbf0912273eb1d888d439b8f5fa506303678aedaddfcebdf81cd4e241fa9.jws": "e76edf83be23339db0a74f992f5a1b2f8e3784f64104aeb56690d649c57fa343",
    "signatures/7d/7d7cb30784fd17b481e0ebd6fef60c45d92cef2da5bb7323b6f97e55a47710f8/deb1afd74f366ab843b81bc3870f11abb66dffa7517fdd52971c7abc52b3971e.jws": "30263e3c0545dd6aa8b60f0863a76632b1923a74f625a50855e16c16a7aa990d",
    "signatures/90/909fce934e0dbeca05f7cac876d38c25fdd6222357642bc310f4c54226b7a073/2e0da03cb6c7b885bf42f009d8a7e29bdb1557df6f981be01b91d5fad718b254.jws": "572218cf69a0f00d152c8060274578d22e8dc272e2b2a509b079fe33097c0bac",
    "tips/32d9ad089a5f11bea1928e8d74e35d1a69f884506e5d4140470ab0bc7e6c7a12.json": "c56ef2e74bd4915861cb71dfae71ffa386c27d76dcf7ad735a79c3747d849327",
    "tips/7d7cb30784fd17b481e0ebd6fef60c45d92cef2da5bb7323b6f97e55a47710f8.json": "c1ce84529c5db807c72743c8b1f975a4d416e67bc959c16364aec05b9a2a2c5c",
    "tips/909fce934e0dbeca05f7cac876d38c25fdd6222357642bc310f4c54226b7a073.json": "61b0104db05290798772f4c80e6b8d7cb3caaf0c33e167b50138276b3a17d07b",
}

# Receipt-level golden: the full on-disk tree ``QueryReceiptStore.record``
# produces for one pinned query, including the receipt JSON and audit mirror.
_GOLDEN_RECEIPT_ID = "sha256:9b28e9c35d2b102b4915b09dc613a764f21a6985c648bd41107e878b453b5e45"

_GOLDEN_RECEIPT_LOG_JSONL = '{"agent_card_kid":"golden-kid-1","agent_id":"agent:golden","artefact_kind":"query-result","artefact_path":".sdd/datasources/queries/sales/2eb4274bbea699a3_74234e98afe7498f.jsonl","content_hash":"sha256:539251727489ad89b71c4b7a750707ed3422f456c31bbd47ccf60e092a4bca1d","operator_hmac":"16bc870c4aba895934dfe362eb5489079926e5d0fb70143031397d8ec0541635","parent_hashes":[],"span_id":"426a558010060f244a7b19ba83eb12a024b139c2dd4ab654e1c3d7e6c376cacf","tool_call_id":"query:sales","ts_ns":1700000000000000001,"v":1}\n'

_GOLDEN_RECEIPT_JSON = '{\n  "artefact_path": ".sdd/datasources/queries/sales/2eb4274bbea699a3_74234e98afe7498f.jsonl",\n  "binding": "426a558010060f244a7b19ba83eb12a024b139c2dd4ab654e1c3d7e6c376cacf",\n  "connection_id": "sales",\n  "content_hash": "sha256:539251727489ad89b71c4b7a750707ed3422f456c31bbd47ccf60e092a4bca1d",\n  "driver": "sqlite",\n  "engine": "sqlite",\n  "executed_at_ns": 1700000000000000001,\n  "lineage_entry_hash": "sha256:9b28e9c35d2b102b4915b09dc613a764f21a6985c648bd41107e878b453b5e45",\n  "params": null,\n  "params_hash": "sha256:74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b",\n  "query_text": "SELECT id, name FROM t ORDER BY id",\n  "query_text_hash": "sha256:2eb4274bbea699a3e8976c75ad2da578ef3e0135c7d070e690cb44a741da00ba",\n  "receipt_id": "sha256:9b28e9c35d2b102b4915b09dc613a764f21a6985c648bd41107e878b453b5e45",\n  "result_copy_relpath": "results/539251727489ad89b71c4b7a750707ed3422f456c31bbd47ccf60e092a4bca1d.bin",\n  "row_cap": 10000,\n  "row_count": 2,\n  "truncated": false,\n  "v": 1\n}'

_GOLDEN_RECEIPT_AUDIT_JSONL = '{"connection_id": "sales", "content_hash": "sha256:539251727489ad89b71c4b7a750707ed3422f456c31bbd47ccf60e092a4bca1d", "event": "datasource.query_receipt", "params_hash": "sha256:74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b", "query_text_hash": "sha256:2eb4274bbea699a3e8976c75ad2da578ef3e0135c7d070e690cb44a741da00ba", "receipt_id": "sha256:9b28e9c35d2b102b4915b09dc613a764f21a6985c648bd41107e878b453b5e45", "row_count": 2, "timestamp": 1700000000.0, "truncated": false}\n'

_GOLDEN_RECEIPT_JWS = "eyJhbGciOiJFZERTQSIsImI2NCI6ZmFsc2UsImNyaXQiOlsiYjY0Il0sImtpZCI6ImdvbGRlbi1raWQtMSJ9..YSXmB-VWEvGYY4veBn_pyzkta6kEW6_uOIH1sd5ZLxaQCVvAvm5296TxYu6gzsVWSbVnn02rAkgR7pdgjXY7Bg"

_GOLDEN_RECEIPT_TREE_SHA256 = {
    "identity/agent:golden/card.json": "600909af55c532baf2f58863ecefac3fc6cc42e3aac7a7799d53d533c0dab738",
    "lineage/by-artefact/72/72725e594c28d45e66e3d702d69da6f2d24541e913ecb5515481d95df844452e.jsonl": "b402ea31850cfe166a0ac03b858929c7da6d97361c3df3a1944248da03d65dba",
    "lineage/log.jsonl": "b402ea31850cfe166a0ac03b858929c7da6d97361c3df3a1944248da03d65dba",
    "lineage/signatures/72/72725e594c28d45e66e3d702d69da6f2d24541e913ecb5515481d95df844452e/9b28e9c35d2b102b4915b09dc613a764f21a6985c648bd41107e878b453b5e45.jws": "1f7bb73e776c1e132e5e4153319e1336a6430e743ac0c34e49482c9a6acffc9e",
    "lineage/tips/72725e594c28d45e66e3d702d69da6f2d24541e913ecb5515481d95df844452e.json": "a38b41d8714cc0aa815fe9eb9c46090ef7ce7129d872e6db3107c9bf6688a642",
    "receipts-audit.jsonl": "e6d1cb788a43b3dfbe4a51eb7c9abab6e423327feeec8427c5f19b039c0908f7",
    "receipts/9b28e9c35d2b102b4915b09dc613a764f21a6985c648bd41107e878b453b5e45.json": "b15620a887f433a36b208ed18f92cf794ff80da1937ee6dfdb5e87d98ae4bff6",
    "results/539251727489ad89b71c4b7a750707ed3422f456c31bbd47ccf60e092a4bca1d.bin": "539251727489ad89b71c4b7a750707ed3422f456c31bbd47ccf60e092a4bca1d",
}


# --- helpers ----------------------------------------------------------------


def _tree_digest(root: Path) -> dict[str, str]:
    """Return ``{relpath: sha256}`` for every file under *root*."""
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _seal_all(store: LineageStore) -> list[str]:
    """Replay the pinned writes through the module-level primitive."""
    return [
        seal_write(
            store,
            _OPERATOR_KEY,
            agent_id=_AGENT_ID,
            agent_card=_CARD,
            private_key_pem=_PRIVATE_PEM,
            **write,
        )
        for write in _WRITES
    ]


@pytest.fixture
def sealed(tmp_path: Path) -> tuple[Path, list[str]]:
    root = tmp_path / "lineage"
    hashes = _seal_all(LineageStore(root))
    return root, hashes


# --- primitive-level byte identity ------------------------------------------


def test_entry_hashes_match_the_golden(sealed: tuple[Path, list[str]]) -> None:
    _, hashes = sealed
    assert hashes == _GOLDEN_ENTRY_HASHES


def test_log_jsonl_is_byte_identical(sealed: tuple[Path, list[str]]) -> None:
    root, _ = sealed
    assert (root / "log.jsonl").read_bytes() == _GOLDEN_LOG_JSONL.encode("utf-8")


def test_jws_sidecars_are_byte_identical(sealed: tuple[Path, list[str]]) -> None:
    """The Ed25519 detached signatures, at their exact sidecar paths."""
    root, _ = sealed
    produced = {
        p.relative_to(root).as_posix(): p.read_text(encoding="utf-8")
        for p in sorted((root / "signatures").rglob("*.jws"))
    }
    assert produced == _GOLDEN_JWS


def test_tip_projections_are_byte_identical(sealed: tuple[Path, list[str]]) -> None:
    root, _ = sealed
    produced = {
        p.relative_to(root).as_posix(): p.read_text(encoding="utf-8") for p in sorted((root / "tips").rglob("*.json"))
    }
    assert produced == _GOLDEN_TIPS


def test_whole_store_tree_is_byte_identical(sealed: tuple[Path, list[str]]) -> None:
    """Every file the store lays down, including the by-artefact projections."""
    root, _ = sealed
    assert _tree_digest(root) == _GOLDEN_TREE_SHA256


def test_golden_signatures_still_verify(sealed: tuple[Path, list[str]]) -> None:
    """The pinned bytes are not just stable - they are valid signatures."""
    root, _ = sealed
    store = LineageStore(root)
    checked = 0
    for entry, jws in store.read_log():
        assert jws, f"missing sidecar for {entry.artefact_path}"
        assert verify_detached(canonicalise(entry), jws, _CARD)
        assert "sha256:" + hashlib.sha256(canonicalise(entry)).hexdigest() == entry_hash(entry)
        checked += 1
    assert checked == len(_WRITES)


def test_object_form_and_deprecated_shim_produce_the_same_bytes(tmp_path: Path) -> None:
    """``SignedLineageLog``, its deprecated ``LineageRecorder`` shim and the
    module-level primitive are three doors onto one substrate."""
    from bernstein.core.lineage.recorder import LineageRecorder

    roots: list[Path] = []
    for name, factory in (
        ("log", SignedLineageLog),
        ("shim", LineageRecorder),
    ):
        root = tmp_path / name
        writer = factory(LineageStore(root), operator_hmac_key=_OPERATOR_KEY)
        for write in _WRITES:
            writer.record_write(
                agent_id=_AGENT_ID,
                agent_card=_CARD,
                private_key_pem=_PRIVATE_PEM,
                **write,
            )
        roots.append(root)

    assert _tree_digest(roots[0]) == _GOLDEN_TREE_SHA256
    assert _tree_digest(roots[1]) == _GOLDEN_TREE_SHA256


# --- receipt-level byte identity --------------------------------------------


class _FixedClock:
    """Stand-in for the ``time`` module inside the signed-write path."""

    @staticmethod
    def time_ns() -> int:
        return _FIXED_NS


@pytest.fixture
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(signed_write_module, "time", _FixedClock)
    yield


def _make_db(path: Path) -> str:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER, name TEXT)")
    conn.executemany("INSERT INTO t VALUES (?, ?)", [(1, "a"), (2, "b")])
    conn.commit()
    conn.close()
    return str(path)


def _record_golden_receipt(tmp_path: Path) -> tuple[Path, Any]:
    db = _make_db(tmp_path / "g.db")
    root = tmp_path / "datasources"
    store = QueryReceiptStore(
        root,
        agent_card=_CARD,
        private_key_pem=_PRIVATE_PEM,
        operator_hmac_key=_OPERATOR_KEY,
    )
    connection = DataSourceConnection(id="sales", driver="sqlite", dsn=db)
    sql = "SELECT id, name FROM t ORDER BY id"
    result = connection.open_engine().execute(sql, row_cap=10_000)
    receipt = store.record(
        connection=connection,
        query_text=sql,
        params=None,
        result=result,
        store_result_copy=True,
        now_ns=_FIXED_NS,
    )
    return root, receipt


@pytest.mark.usefixtures("fixed_clock")
def test_query_receipt_tree_is_byte_identical(tmp_path: Path) -> None:
    """The migrated datasource receipt path lays down the same bytes as before."""
    root, receipt = _record_golden_receipt(tmp_path)
    assert receipt.receipt_id == _GOLDEN_RECEIPT_ID
    assert (root / "lineage" / "log.jsonl").read_bytes() == _GOLDEN_RECEIPT_LOG_JSONL.encode("utf-8")
    assert (root / "receipts-audit.jsonl").read_bytes() == _GOLDEN_RECEIPT_AUDIT_JSONL.encode("utf-8")
    receipt_path = root / "receipts" / f"{_GOLDEN_RECEIPT_ID.split(':', 1)[1]}.json"
    assert receipt_path.read_bytes() == _GOLDEN_RECEIPT_JSON.encode("utf-8")
    (jws_path,) = sorted((root / "lineage" / "signatures").rglob("*.jws"))
    assert jws_path.read_text(encoding="utf-8") == _GOLDEN_RECEIPT_JWS
    assert _tree_digest(root) == _GOLDEN_RECEIPT_TREE_SHA256


@pytest.mark.usefixtures("fixed_clock")
def test_query_receipt_still_verifies_against_the_golden_bytes(tmp_path: Path) -> None:
    """Byte identity is worth nothing if ``verify()`` stopped meaning anything."""
    root, receipt = _record_golden_receipt(tmp_path)
    store = QueryReceiptStore(
        root,
        agent_card=_CARD,
        private_key_pem=_PRIVATE_PEM,
        operator_hmac_key=_OPERATOR_KEY,
    )
    outcome = store.verify(receipt.receipt_id)
    assert outcome.ok, outcome.failures
    assert outcome.checks["signature"] is True
    assert outcome.checks["operator_hmac"] is True
    assert outcome.checks["receipt_body"] is True


@pytest.mark.usefixtures("fixed_clock")
def test_golden_receipt_log_line_is_canonical_json(tmp_path: Path) -> None:
    """The pinned log line is the JCS form, not merely a stable string."""
    root, _ = _record_golden_receipt(tmp_path)
    raw = (root / "lineage" / "log.jsonl").read_bytes().rstrip(b"\n")
    payload = json.loads(raw)
    recanonicalised = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert recanonicalised.encode("utf-8") == raw
