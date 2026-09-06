"""Regression test for issue #5095: one audit-key loader, not twenty.

``def _load_hmac_key`` was defined twenty times under ``src/bernstein/cli``.
Nineteen were byte-identical two-line wrappers around
:func:`bernstein.core.security.audit.load_or_create_audit_key`; the twentieth,
in ``lineage_verify_cmd``, differed on purpose because a ``verify`` pass must
never mint key material (issue #2639).

Nineteen copies of a two-line wrapper is nineteen places a later edit -- a
key-rotation check, a different error message -- has to land correctly, and it
usually will not. This test locks the post-fix invariant: no module defines its
own private loader, so the one deliberate divergence stays visible as a named
helper rather than hiding as wrapper number fourteen.
"""

from __future__ import annotations

import ast
from pathlib import Path

from bernstein.cli.helpers import load_verify_only_key
from bernstein.core.security.audit import load_audit_key, load_or_create_audit_key

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "bernstein"


def _private_loader_definitions() -> list[str]:
    """Every ``def _load_hmac_key`` under ``src/bernstein``, as ``path:line``.

    Walked as an AST rather than grepped so a name inside a string or a comment
    cannot fail the test, and so a nested or method definition is still caught.
    """
    found: list[str] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == "_load_hmac_key":
                found.append(f"{path.relative_to(SRC_ROOT)}:{node.lineno}")
    return found


def test_no_duplicate_load_hmac_key_definitions() -> None:
    """No module defines a private ``_load_hmac_key``.

    Writing commands import ``load_or_create_audit_key`` directly; the one
    read-only caller uses the named ``load_verify_only_key`` helper.
    """
    definitions = _private_loader_definitions()
    assert definitions == [], (
        "private _load_hmac_key wrappers are back (issue #5095) -- import "
        "load_or_create_audit_key directly, or load_verify_only_key for a "
        f"read-only verify: {definitions}"
    )


def test_writing_commands_import_the_canonical_loader() -> None:
    """The command modules that had a wrapper now bind the canonical function."""
    from importlib import import_module

    for name in ("evidence_cmd", "governance_cmd", "gate_cmd", "mandate_cmd", "skill_cmd"):
        module = import_module(f"bernstein.cli.commands.{name}")
        assert module.load_or_create_audit_key is load_or_create_audit_key, (
            f"{name} no longer resolves to the canonical audit-key loader"
        )


def test_lineage_verify_uses_read_only_key_loader() -> None:
    """The verify path never reaches the key-creating loader.

    A verifier that minted its own key would fail every HMAC check against a
    chain written under the real key and report that as tampering, turning a
    missing-key operator error into a false integrity alarm (issue #2639).
    """
    from bernstein.cli.commands import lineage_verify_cmd

    assert lineage_verify_cmd.load_verify_only_key is load_verify_only_key
    assert not hasattr(lineage_verify_cmd, "load_or_create_audit_key")

    source = (SRC_ROOT / "cli" / "commands" / "lineage_verify_cmd.py").read_text(encoding="utf-8")
    assert "load_or_create_audit_key" not in source


def test_verify_only_loader_is_the_read_only_one() -> None:
    """``load_verify_only_key`` delegates to ``load_audit_key``, not its creating sibling."""
    import inspect

    body = inspect.getsource(load_verify_only_key)
    assert "load_audit_key(key_path)" in body
    assert "load_or_create_audit_key(" not in body
    assert load_audit_key is not load_or_create_audit_key
