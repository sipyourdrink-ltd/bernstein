"""Byte-preserving baseline canonicalizer consolidation for issue #5094."""

from __future__ import annotations

import ast
import inspect
from importlib import import_module
from typing import Any

import pytest

from bernstein.core.evidence import bundle
from bernstein.core.review import receipt
from bernstein.core.security import governance

_MODULES = (bundle, governance, receipt)
_CALLERS = (
    bundle.canonical_bytes,
    bundle._canonical_bytes,
    governance._canonical_bytes,
    receipt._canonical_bytes,
)
_CALLER_IDS = ("evidence-public", "evidence-compat", "governance", "review")


@pytest.mark.parametrize("module", _MODULES, ids=("evidence", "governance", "review"))
def test_baseline_modules_do_not_define_canonicalizer_bodies(module: Any) -> None:
    tree = ast.parse(inspect.getsource(module))
    bodies = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name in {"canonical_bytes", "_canonical_bytes"}
    ]
    assert bodies == [], f"{module.__name__} still defines canonicalizer bodies: {bodies}"


@pytest.mark.parametrize("caller", _CALLERS, ids=_CALLER_IDS)
def test_legacy_names_are_shared_helper_aliases(caller: Any) -> None:
    canonicalization = import_module("bernstein.core.canonicalization")
    assert caller is canonicalization.canonical_bytes


@pytest.mark.parametrize("caller", _CALLERS, ids=_CALLER_IDS)
@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        pytest.param(
            {"z": [{"é": "雪", "a": "café"}], "a": "שלום"},
            '{"a":"שלום","z":[{"a":"café","é":"雪"}]}'.encode(),
            id="nested-non-ascii",
        ),
        pytest.param(
            {"😀": "𐐀", "\ue000": "private", "a": "🚀"},
            '{"a":"🚀","\ue000":"private","😀":"𐐀"}'.encode(),
            id="astral-code-point-key-order",
        ),
        pytest.param(
            {"z": [10.0, -0.0, 1e-7, 1e20, 1e21], "a": 1.25},
            b'{"a":1.25,"z":[10.0,-0.0,1e-07,1e+20,1e+21]}',
            id="python-float-spellings",
        ),
        pytest.param(
            {"nan": float("nan"), "inf": float("inf"), "neg": -float("inf"), "big": 2**64},
            b'{"big":18446744073709551616,"inf":Infinity,"nan":NaN,"neg":-Infinity}',
            id="legacy-nonfinite-and-large-integer",
        ),
        pytest.param({}, b"{}", id="empty-object"),
        pytest.param(
            {"z": [True, None, False], "a": 'quote" slash\\ newline\n tab\t null\x00'},
            b'{"a":"quote\\" slash\\\\ newline\\n tab\\t null\\u0000","z":[true,null,false]}',
            id="escaping-and-json-literals",
        ),
    ],
)
def test_legacy_callers_preserve_fixed_bytes(caller: Any, payload: dict[str, Any], expected: bytes) -> None:
    assert caller(payload) == expected


@pytest.mark.parametrize("caller", _CALLERS, ids=_CALLER_IDS)
def test_legacy_callers_preserve_lone_surrogate_failure(caller: Any) -> None:
    with pytest.raises(UnicodeEncodeError):
        caller({"text": "\ud800"})
