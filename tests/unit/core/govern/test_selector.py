"""Tests for the govern inventory selector grammar and typed resolution API."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from bernstein.core.govern.selector import (
    AliasTable,
    GroupConflictError,
    InventoryGroup,
    InventoryNode,
    InventoryStore,
    Projection,
    Selector,
    SelectorError,
    SelectorSyntaxError,
    resolve_targets,
)

_SELECTOR_MODULE = "bernstein.core.govern.selector"


def _store() -> InventoryStore:
    """A small fixture store: five nodes, three groups, one per-node override."""
    return InventoryStore(
        nodes=(
            InventoryNode(
                node_id="arn:aws:s3:::logs",
                attributes={
                    "kind": ("bucket",),
                    "region": ("us-east-1",),
                    "tier": ("cold", "archive"),
                    "retention": ("365d",),
                },
                groups=("prod",),
            ),
            InventoryNode(
                node_id="arn:aws:s3:::assets",
                attributes={"kind": ("bucket",), "region": ("us-west-2",)},
                groups=("prod",),
            ),
            InventoryNode(
                node_id="arn:aws:iam::role/deploy",
                attributes={"kind": ("role",), "region": ("global",)},
                groups=("prod", "privileged"),
            ),
            InventoryNode(
                node_id="arn:aws:s3:::scratch",
                attributes={"kind": ("bucket",), "region": ("eu-central-1",), "retention": ("7d",)},
                groups=("dev",),
            ),
            InventoryNode(
                node_id="arn:aws:s3:::backup",
                attributes={"kind": ("bucket",), "region": ("us-east-1",)},
                groups=(),
            ),
        ),
        groups=(
            InventoryGroup(group_id="prod", values={"retention": ("90d",), "env": ("production",)}),
            InventoryGroup(group_id="dev", values={"env": ("development",)}),
            InventoryGroup(group_id="privileged", values={"review": ("two-person",)}),
        ),
    )


class TestDeterminism:
    """The same selector over the same store returns the same ordered result."""

    def test_same_selector_same_store_same_order(self) -> None:
        store = _store()
        selector = Selector.parse(["kind", "bucket"])

        first = resolve_targets(store, selector)
        second = resolve_targets(store, selector)
        # A store whose nodes were declared in a different order must not
        # reorder the result: the order is a property of the selector, not of
        # how the store happened to serialize its rows.
        shuffled = InventoryStore(nodes=tuple(reversed(store.nodes)), groups=tuple(reversed(store.groups)))
        third = resolve_targets(shuffled, selector)

        ids = [n.node_id for n in first]
        assert ids == [n.node_id for n in second]
        assert ids == [n.node_id for n in third]
        assert ids == sorted(ids)
        assert ids == [
            "arn:aws:s3:::assets",
            "arn:aws:s3:::backup",
            "arn:aws:s3:::logs",
            "arn:aws:s3:::scratch",
        ]


class TestFilterChain:
    """Successive ``key value`` filters: exact, regex and set membership."""

    def test_selector_chain_matches_exact_regex_and_set(self) -> None:
        store = _store()
        selector = Selector.parse(
            [
                "kind",
                "bucket",  # exact
                "region",
                "~^us-",  # regex
                "group",
                "{prod,staging}",  # set membership
            ]
        )

        matched = [n.node_id for n in resolve_targets(store, selector)]

        # backup is us-east-1 but in no group; scratch is in dev and eu-central-1.
        assert matched == ["arn:aws:s3:::assets", "arn:aws:s3:::logs"]

    def test_filter_matches_any_value_of_a_multi_valued_attribute(self) -> None:
        store = _store()
        matched = [n.node_id for n in resolve_targets(store, Selector.parse(["tier", "archive"]))]
        assert matched == ["arn:aws:s3:::logs"]

    def test_escaped_value_is_matched_literally(self) -> None:
        store = InventoryStore(
            nodes=(InventoryNode(node_id="n1", attributes={"pattern": ("~^us-",)}, groups=()),),
            groups=(),
        )
        assert [n.node_id for n in resolve_targets(store, Selector.parse(["pattern", r"\~^us-"]))] == ["n1"]

    def test_odd_token_count_is_a_syntax_error(self) -> None:
        with pytest.raises(SelectorSyntaxError):
            Selector.parse(["kind", "bucket", "region"])

    def test_unknown_alias_is_a_syntax_error(self) -> None:
        with pytest.raises(SelectorSyntaxError):
            Selector.parse(["@nosuchalias"], aliases=AliasTable.empty())


class TestAliases:
    """Aliases are declared as data, not code."""

    def test_alias_expands_to_declared_filter_pairs(self, tmp_path: Path) -> None:
        (tmp_path / "site.json").write_text(
            json.dumps({"aliases": {"us-buckets": ["kind", "bucket", "region", "~^us-"]}}),
            encoding="utf-8",
        )
        aliases = AliasTable.from_directory(tmp_path)
        store = _store()

        via_alias = resolve_targets(store, Selector.parse(["@us-buckets"], aliases=aliases))
        written_out = resolve_targets(store, Selector.parse(["kind", "bucket", "region", "~^us-"]))

        assert [n.node_id for n in via_alias] == [n.node_id for n in written_out]
        assert [n.node_id for n in via_alias] == [
            "arn:aws:s3:::assets",
            "arn:aws:s3:::backup",
            "arn:aws:s3:::logs",
        ]

    def test_alias_cycle_is_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "loop.json").write_text(
            json.dumps({"aliases": {"a": ["@b"], "b": ["@a"]}}),
            encoding="utf-8",
        )
        aliases = AliasTable.from_directory(tmp_path)
        with pytest.raises(SelectorSyntaxError, match="cycle"):
            Selector.parse(["@a"], aliases=aliases)

    def test_alias_declared_twice_in_a_directory_is_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "a.json").write_text(json.dumps({"aliases": {"prod": ["env", "production"]}}), encoding="utf-8")
        (tmp_path / "b.json").write_text(json.dumps({"aliases": {"prod": ["env", "prod"]}}), encoding="utf-8")
        with pytest.raises(SelectorSyntaxError, match="prod"):
            AliasTable.from_directory(tmp_path)


class TestGroupEdges:
    """Group membership is an edge; group values resolve onto members."""

    def test_group_value_resolves_to_members_with_override(self) -> None:
        store = _store()
        resolved = {n.node_id: n for n in resolve_targets(store, Selector.parse(["group", "prod"]))}

        # assets declares no retention, so it inherits the prod group's value.
        assert resolved["arn:aws:s3:::assets"].attributes["retention"] == ("90d",)
        # logs declares its own, which wins over the group-level value.
        assert resolved["arn:aws:s3:::logs"].attributes["retention"] == ("365d",)
        # scratch is not in prod, so it is not in this result at all.
        assert "arn:aws:s3:::scratch" not in resolved

    def test_selector_matches_an_inherited_group_value(self) -> None:
        store = _store()
        matched = [n.node_id for n in resolve_targets(store, Selector.parse(["env", "production"]))]
        assert matched == [
            "arn:aws:iam::role/deploy",
            "arn:aws:s3:::assets",
            "arn:aws:s3:::logs",
        ]

    def test_conflicting_group_values_without_override_are_rejected(self) -> None:
        store = InventoryStore(
            nodes=(InventoryNode(node_id="n1", attributes={}, groups=("left", "right")),),
            groups=(
                InventoryGroup(group_id="left", values={"retention": ("7d",)}),
                InventoryGroup(group_id="right", values={"retention": ("90d",)}),
            ),
        )
        with pytest.raises(GroupConflictError) as excinfo:
            resolve_targets(store, Selector.parse(["id", "n1"]))
        message = str(excinfo.value)
        assert "n1" in message
        assert "retention" in message
        assert "left" in message
        assert "right" in message

    def test_membership_in_an_undeclared_group_is_rejected(self) -> None:
        store = InventoryStore(
            nodes=(InventoryNode(node_id="n1", attributes={}, groups=("ghost",)),),
            groups=(),
        )
        with pytest.raises(SelectorError, match="ghost"):
            resolve_targets(store, Selector.parse(["id", "n1"]))

    def test_node_override_resolves_a_group_conflict(self) -> None:
        store = InventoryStore(
            nodes=(InventoryNode(node_id="n1", attributes={"retention": ("30d",)}, groups=("left", "right")),),
            groups=(
                InventoryGroup(group_id="left", values={"retention": ("7d",)}),
                InventoryGroup(group_id="right", values={"retention": ("90d",)}),
            ),
        )
        resolved = resolve_targets(store, Selector.parse(["id", "n1"]))
        assert resolved[0].attributes["retention"] == ("30d",)


class TestProjection:
    """``%field %field`` renders one line per match; JSON carries only those fields."""

    def test_projection_renders_template_line_and_json_fields(self) -> None:
        store = _store()
        matched = resolve_targets(store, Selector.parse(["group", "dev"]))
        projection = Projection.parse("%id in %region keeps %retention")

        assert [projection.render(n) for n in matched] == ["arn:aws:s3:::scratch in eu-central-1 keeps 7d"]
        assert [projection.project(n) for n in matched] == [
            {"id": "arn:aws:s3:::scratch", "region": "eu-central-1", "retention": "7d"}
        ]

    def test_projection_of_a_missing_field_is_empty_not_an_error(self) -> None:
        store = _store()
        matched = resolve_targets(store, Selector.parse(["id", "arn:aws:s3:::backup"]))
        projection = Projection.parse("%id|%retention")
        assert projection.render(matched[0]) == "arn:aws:s3:::backup|"
        assert projection.project(matched[0]) == {"id": "arn:aws:s3:::backup", "retention": ""}

    def test_projection_without_a_field_is_a_syntax_error(self) -> None:
        with pytest.raises(SelectorSyntaxError):
            Projection.parse("no fields here")


class TestTypedCallSites:
    """Consumers resolve targets through the typed API, never a built string."""

    def test_call_sites_use_typed_api_not_raw_strings(self) -> None:
        # A bare string is also a Sequence[str] -- of its characters -- so
        # accepting one would silently parse "kind bucket" into per-character
        # filters. Rejecting it is what keeps the shell-string path structurally
        # unavailable to every consumer.
        with pytest.raises(TypeError):
            Selector.parse("kind bucket")  # type: ignore[arg-type]

        src_root = Path(__file__).resolve().parents[4] / "src" / "bernstein"
        assert src_root.is_dir(), src_root

        offenders: list[str] = []
        for path in sorted(src_root.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            if _SELECTOR_MODULE not in source:
                continue
            tree = ast.parse(source, filename=str(path))
            for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
                if not _is_selector_entry_point(call.func):
                    continue
                for arg in [*call.args, *(kw.value for kw in call.keywords)]:
                    if _is_constructed_string(arg):
                        offenders.append(f"{path}:{call.lineno} builds a selector argument as a string")
        assert offenders == []


def _is_selector_entry_point(func: ast.expr) -> bool:
    """True when *func* names one of the typed resolution entry points."""
    if isinstance(func, ast.Name):
        return func.id == "resolve_targets"
    if isinstance(func, ast.Attribute):
        owner = func.value
        if func.attr == "resolve_targets":
            return True
        return func.attr == "parse" and isinstance(owner, ast.Name) and owner.id in {"Selector", "Projection"}
    return False


def _is_constructed_string(node: ast.expr) -> bool:
    """True when *node* is a string assembled at the call site."""
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add | ast.Mod):
        return isinstance(node.left, ast.Constant) and isinstance(node.left.value, str)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        return node.func.attr in {"format", "join"}
    return False
