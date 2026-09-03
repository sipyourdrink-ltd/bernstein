"""Tests for the install-wide plugin/skill pin manifest (issue #5089).

The manifest is a governed allow-list: it names every plugin and skill the
install may load, each at an exact version and content address, plus the
sources each environment may load them from. These tests protect four
properties:

1. A floating version specifier is rejected when the manifest is parsed,
   not warned about later.
2. Both plugins and skills carry an exact version and a content hash.
3. ``bernstein verify pins`` exits non-zero and prints every drifted entry.
4. Applying the manifest twice is idempotent and each apply appends a
   decision record carrying the manifest hash before and after.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.plugins_core.plugin_pin_manifest import (
    PIN_KIND_PLUGIN,
    PIN_KIND_SKILL,
    LoadedComponent,
    PinManifestError,
    apply_pin_manifest,
    load_pin_manifest,
    parse_pin_manifest,
    read_apply_records,
    verify_pinned_set,
)

_PLUGIN_HASH = "sha256:" + "a" * 64
_SKILL_HASH = "sha256:" + "b" * 64


def _manifest_mapping() -> dict[str, object]:
    """A minimal well-formed manifest covering one plugin and one skill."""
    return {
        "version": 1,
        "environments": {
            "production": {"allowed_sources": ["github://acme/plugins"]},
        },
        "plugins": [
            {
                "name": "audit-logger",
                "version": "2.0.0",
                "content_hash": _PLUGIN_HASH,
                "source": "github://acme/plugins",
            }
        ],
        "skills": [
            {
                "name": "code-review",
                "version": "1.2.0",
                "content_hash": _SKILL_HASH,
                "source": "github://acme/plugins",
            }
        ],
    }


def _loaded_set() -> list[LoadedComponent]:
    """The resolved set that exactly matches :func:`_manifest_mapping`."""
    return [
        LoadedComponent(
            kind=PIN_KIND_PLUGIN,
            name="audit-logger",
            version="2.0.0",
            content_hash=_PLUGIN_HASH,
            source="github://acme/plugins",
        ),
        LoadedComponent(
            kind=PIN_KIND_SKILL,
            name="code-review",
            version="1.2.0",
            content_hash=_SKILL_HASH,
            source="github://acme/plugins",
        ),
    ]


# ---------------------------------------------------------------------------
# 1. Parse-time rejection of floating versions (load-bearing)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "floating",
    ["latest", "*", "^1.2.0", "~1.2.0", ">=1.0.0", "1.2", "1.2.x", "main", ""],
)
def test_manifest_rejects_floating_or_latest_version_at_parse_time(floating: str) -> None:
    """A non-exact version must fail the parse, not survive it as a warning."""
    data = _manifest_mapping()
    plugins = data["plugins"]
    assert isinstance(plugins, list)
    entry = plugins[0]
    assert isinstance(entry, dict)
    entry["version"] = floating

    with pytest.raises(PinManifestError) as excinfo:
        parse_pin_manifest(data)

    assert any("audit-logger" in err for err in excinfo.value.errors)
    assert any("version" in err for err in excinfo.value.errors)


def test_manifest_rejects_a_floating_skill_version_too() -> None:
    """The rejection is a property of every entry, not only of plugins."""
    data = _manifest_mapping()
    skills = data["skills"]
    assert isinstance(skills, list)
    entry = skills[0]
    assert isinstance(entry, dict)
    entry["version"] = "latest"

    with pytest.raises(PinManifestError) as excinfo:
        parse_pin_manifest(data)

    assert any("code-review" in err for err in excinfo.value.errors)


def test_manifest_rejects_a_content_hash_that_is_not_a_sha256_address() -> None:
    """A pin without a full ``sha256:<64 hex>`` address is not a pin."""
    data = _manifest_mapping()
    plugins = data["plugins"]
    assert isinstance(plugins, list)
    entry = plugins[0]
    assert isinstance(entry, dict)
    entry["content_hash"] = "sha256:deadbeef"

    with pytest.raises(PinManifestError) as excinfo:
        parse_pin_manifest(data)

    assert any("content_hash" in err for err in excinfo.value.errors)


def test_manifest_rejects_a_source_no_environment_allows() -> None:
    """An entry whose source is listed by no environment cannot be loaded anywhere."""
    data = _manifest_mapping()
    plugins = data["plugins"]
    assert isinstance(plugins, list)
    entry = plugins[0]
    assert isinstance(entry, dict)
    entry["source"] = "github://someone-else/plugins"

    with pytest.raises(PinManifestError) as excinfo:
        parse_pin_manifest(data)

    assert any("source" in err for err in excinfo.value.errors)


# ---------------------------------------------------------------------------
# 2. Coverage: every plugin and skill, exact version, content hash
# ---------------------------------------------------------------------------


def test_manifest_lists_every_plugin_and_skill_with_exact_version_and_content_hash(tmp_path: Path) -> None:
    """One manifest carries both subsystems; each entry is exactly pinned."""
    path = tmp_path / "pins.yaml"
    path.write_text(json.dumps(_manifest_mapping()), encoding="utf-8")

    manifest = load_pin_manifest(path)

    kinds = {(e.kind, e.name) for e in manifest.entries}
    assert kinds == {(PIN_KIND_PLUGIN, "audit-logger"), (PIN_KIND_SKILL, "code-review")}
    for entry in manifest.entries:
        assert entry.version.count(".") == 2
        assert entry.content_hash.startswith("sha256:")
        assert len(entry.content_hash) == len("sha256:") + 64
        assert entry.source
    assert manifest.allowed_sources("production") == frozenset({"github://acme/plugins"})


def test_manifest_hash_is_stable_across_entry_order(tmp_path: Path) -> None:
    """The manifest hash addresses content, not the order it was written in."""
    forward = _manifest_mapping()
    reversed_data = _manifest_mapping()
    plugins = reversed_data["plugins"]
    skills = reversed_data["skills"]
    assert isinstance(plugins, list)
    assert isinstance(skills, list)
    plugins.append(
        {
            "name": "aaa-first",
            "version": "0.1.0",
            "content_hash": "sha256:" + "c" * 64,
            "source": "github://acme/plugins",
        }
    )
    forward_plugins = forward["plugins"]
    assert isinstance(forward_plugins, list)
    forward_plugins.insert(
        0,
        {
            "name": "aaa-first",
            "version": "0.1.0",
            "content_hash": "sha256:" + "c" * 64,
            "source": "github://acme/plugins",
        },
    )

    assert parse_pin_manifest(forward).manifest_hash() == parse_pin_manifest(reversed_data).manifest_hash()


# ---------------------------------------------------------------------------
# 3. verify: non-zero exit, every drifted entry printed
# ---------------------------------------------------------------------------


def test_verify_reports_no_drift_when_the_loaded_set_matches_the_manifest() -> None:
    result = verify_pinned_set(parse_pin_manifest(_manifest_mapping()), _loaded_set(), environment="production")
    assert result.ok
    assert result.drifts == ()


def test_verify_exits_nonzero_and_prints_each_drifted_entry_on_divergence(tmp_path: Path) -> None:
    """Three independent divergences must all reach the operator's terminal."""
    from click.testing import CliRunner

    from bernstein.cli.commands.verify_cmd import verify_cmd

    manifest_path = tmp_path / "pins.yaml"
    manifest_path.write_text(json.dumps(_manifest_mapping()), encoding="utf-8")

    loaded_path = tmp_path / "loaded.json"
    loaded_path.write_text(
        json.dumps(
            [
                # version drift
                {
                    "kind": PIN_KIND_PLUGIN,
                    "name": "audit-logger",
                    "version": "2.0.1",
                    "content_hash": _PLUGIN_HASH,
                    "source": "github://acme/plugins",
                },
                # content-hash drift
                {
                    "kind": PIN_KIND_SKILL,
                    "name": "code-review",
                    "version": "1.2.0",
                    "content_hash": "sha256:" + "f" * 64,
                    "source": "github://acme/plugins",
                },
                # presence drift: loaded but not pinned at all
                {
                    "kind": PIN_KIND_PLUGIN,
                    "name": "smuggled",
                    "version": "9.9.9",
                    "content_hash": "sha256:" + "e" * 64,
                    "source": "github://acme/plugins",
                },
            ]
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        verify_cmd,
        [
            "pins",
            "--manifest",
            str(manifest_path),
            "--loaded",
            str(loaded_path),
            "--environment",
            "production",
        ],
    )

    assert result.exit_code != 0
    assert "audit-logger" in result.output
    assert "code-review" in result.output
    assert "smuggled" in result.output


def test_verify_exits_zero_when_the_loaded_set_matches(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from bernstein.cli.commands.verify_cmd import verify_cmd

    manifest_path = tmp_path / "pins.yaml"
    manifest_path.write_text(json.dumps(_manifest_mapping()), encoding="utf-8")
    loaded_path = tmp_path / "loaded.json"
    loaded_path.write_text(
        json.dumps(
            [
                {
                    "kind": c.kind,
                    "name": c.name,
                    "version": c.version,
                    "content_hash": c.content_hash,
                    "source": c.source,
                }
                for c in _loaded_set()
            ]
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        verify_cmd,
        ["pins", "--manifest", str(manifest_path), "--loaded", str(loaded_path), "--environment", "production"],
    )

    assert result.exit_code == 0, result.output


def test_verify_rejects_a_component_from_a_source_the_environment_does_not_allow() -> None:
    """An unlisted source fails regardless of a matching version and hash."""
    manifest = parse_pin_manifest(_manifest_mapping())
    loaded = _loaded_set()
    loaded[0] = LoadedComponent(
        kind=PIN_KIND_PLUGIN,
        name="audit-logger",
        version="2.0.0",
        content_hash=_PLUGIN_HASH,
        source="github://mirror/plugins",
    )

    result = verify_pinned_set(manifest, loaded, environment="production")

    assert not result.ok
    assert result.exit_code != 0
    reasons = {d.reason for d in result.drifts}
    assert "source" in reasons


def test_verify_reports_a_pinned_entry_that_is_not_loaded() -> None:
    """Presence divergence runs both ways: pinned-but-absent is drift too."""
    manifest = parse_pin_manifest(_manifest_mapping())
    result = verify_pinned_set(manifest, _loaded_set()[:1], environment="production")

    assert not result.ok
    assert any(d.name == "code-review" and d.reason == "absent" for d in result.drifts)


def test_verify_rejects_an_unknown_environment_name() -> None:
    manifest = parse_pin_manifest(_manifest_mapping())
    result = verify_pinned_set(manifest, _loaded_set(), environment="staging")

    assert not result.ok
    assert any(d.reason == "environment" for d in result.drifts)


# ---------------------------------------------------------------------------
# 4. Idempotent apply with a before/after decision record
# ---------------------------------------------------------------------------


def test_apply_manifest_twice_is_idempotent_with_decision_record_hash_before_and_after(tmp_path: Path) -> None:
    manifest = parse_pin_manifest(_manifest_mapping())

    first = apply_pin_manifest(manifest, workdir=tmp_path, timestamp=1_000)
    applied_after_first = (tmp_path / ".sdd" / "plugins" / "pins" / "applied.json").read_bytes()

    second = apply_pin_manifest(manifest, workdir=tmp_path, timestamp=2_000)
    applied_after_second = (tmp_path / ".sdd" / "plugins" / "pins" / "applied.json").read_bytes()

    # Idempotent: the applied state is byte-identical and unchanged.
    assert applied_after_first == applied_after_second
    assert first.changed is True
    assert second.changed is False

    # Every apply carries the manifest hash before and after.
    assert first.manifest_hash_before == ""
    assert first.manifest_hash_after == manifest.manifest_hash()
    assert second.manifest_hash_before == manifest.manifest_hash()
    assert second.manifest_hash_after == manifest.manifest_hash()

    # Every apply -- including the no-op -- appends a decision record.
    records = read_apply_records(tmp_path)
    assert len(records) == 2
    assert [r.manifest_hash_before for r in records] == ["", manifest.manifest_hash()]
    assert [r.manifest_hash_after for r in records] == [manifest.manifest_hash()] * 2


def test_apply_records_a_changed_manifest_hash_when_the_pins_move(tmp_path: Path) -> None:
    """A real change records the prior hash, so drift has a before value."""
    first_manifest = parse_pin_manifest(_manifest_mapping())
    apply_pin_manifest(first_manifest, workdir=tmp_path, timestamp=1_000)

    data = _manifest_mapping()
    plugins = data["plugins"]
    assert isinstance(plugins, list)
    entry = plugins[0]
    assert isinstance(entry, dict)
    entry["version"] = "2.0.1"
    second_manifest = parse_pin_manifest(data)

    record = apply_pin_manifest(second_manifest, workdir=tmp_path, timestamp=2_000)

    assert record.changed is True
    assert record.manifest_hash_before == first_manifest.manifest_hash()
    assert record.manifest_hash_after == second_manifest.manifest_hash()
    assert record.manifest_hash_before != record.manifest_hash_after
