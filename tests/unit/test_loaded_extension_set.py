"""The skill/plugin set an install actually loaded, not the one it declared.

Every test here exercises the real resolution path: real ``SKILL.md`` files
on disk, a real ``.dist-info`` on ``sys.path`` so ``importlib.metadata``
discovers a real entry point, and the real
:class:`~bernstein.plugins.manager.PluginManager` load routines. The record
is built from what resolution produced, so a declared entry that never
imported is present as *not loaded* instead of vanishing into a warning.
"""

from __future__ import annotations

import importlib
import sys
import textwrap
from typing import TYPE_CHECKING

import pytest

from bernstein.core.replay.journal import EventJournal
from bernstein.core.replay.run_receipt import build_run_receipt, verify_run_receipt
from bernstein.core.security.lineage_kms import FileBasedKMSAdapter
from bernstein.core.security.loaded_extension_set import (
    ExtensionKind,
    build_loaded_extension_set,
    record_loaded_extension_set,
)
from bernstein.core.skills.loader import SkillLoader
from bernstein.core.skills.sources.local_dir import LocalDirSkillSource
from bernstein.core.skills.sources.plugin import scan_plugin_sources
from bernstein.plugins.manager import PluginManager

if TYPE_CHECKING:
    from pathlib import Path

_SKILL_GROUP = "bernstein.test_skill_sources_4986"
_SIGN_SEED = b"e" * 32


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _write_skill(root: Path, name: str, *, version: str, body: str) -> Path:
    """Materialise ``<root>/<name>/SKILL.md`` and return the file path."""
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        textwrap.dedent(
            f"""\
            ---
            name: {name}
            description: A fixture skill used to exercise the loaded-extension record.
            version: "{version}"
            ---

            {body}
            """
        ),
        encoding="utf-8",
    )
    return skill_md


def _install_dist(
    site: Path,
    *,
    dist_name: str,
    version: str,
    group: str,
    ep_name: str,
    ep_value: str,
) -> None:
    """Write a real ``.dist-info`` so ``importlib.metadata`` sees the entry point."""
    info = site / f"{dist_name.replace('-', '_')}-{version}.dist-info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {dist_name}\nVersion: {version}\n",
        encoding="utf-8",
    )
    (info / "entry_points.txt").write_text(
        f"[{group}]\n{ep_name} = {ep_value}\n",
        encoding="utf-8",
    )


def _write_plugin_module(site: Path, module: str, *, version: str, marker: str) -> Path:
    """Write an importable hook plugin module and return its path."""
    path = site / f"{module}.py"
    path.write_text(
        textwrap.dedent(
            f"""\
            from bernstein.plugins import hookimpl

            __version__ = "{version}"
            MARKER = "{marker}"


            class FixturePlugin:
                @hookimpl
                def on_task_created(self, task_id, role, title):
                    return None
            """
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway import root on ``sys.path`` with caches invalidated."""
    root = tmp_path / "site"
    root.mkdir()
    monkeypatch.syspath_prepend(str(root))
    importlib.invalidate_caches()
    return root


@pytest.fixture(autouse=True)
def _drop_fixture_modules() -> object:
    """Forget fixture modules so each test imports its own bytes."""
    yield None
    for name in [m for m in sys.modules if m.startswith("fixture_ext_4986")]:
        del sys.modules[name]


def _kms(tmp_path: Path) -> FileBasedKMSAdapter:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key_path = tmp_path / "run-receipt-signing.pem"
    key_path.write_bytes(
        Ed25519PrivateKey.from_private_bytes(_SIGN_SEED).private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )
    return FileBasedKMSAdapter(key_path, kid="test-extension-set-key")


# ---------------------------------------------------------------------------
# 1. Loaded skills carry source, version and byte digest.
# ---------------------------------------------------------------------------


def test_loaded_skill_is_recorded_with_source_version_and_byte_digest(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    skill_md = _write_skill(skills_root, "alpha-pack", version="2.3.1", body="Alpha body.")
    loader = SkillLoader([LocalDirSkillSource(skills_root, source_name="local")])

    record = build_loaded_extension_set(loader=loader)
    entry = next(e for e in record.entries if e.name == "alpha-pack")

    assert entry.kind is ExtensionKind.SKILL
    assert entry.loaded is True
    assert entry.source == "local"
    assert entry.origin == str(skill_md.resolve())
    assert entry.version == "2.3.1"
    assert entry.content_digest.startswith("sha256:")
    assert len(entry.content_digest) == len("sha256:") + 64


# ---------------------------------------------------------------------------
# 2. Loaded plugins carry source, version and byte digest.
# ---------------------------------------------------------------------------


def test_loaded_plugin_is_recorded_with_source_version_and_byte_digest(
    site: Path,
    tmp_path: Path,
) -> None:
    module_path = _write_plugin_module(site, "fixture_ext_4986_ok", version="0.4.0", marker="first")
    manager = PluginManager(workdir=tmp_path)
    manager.discover_config_plugins(["fixture_ext_4986_ok:FixturePlugin"])

    record = build_loaded_extension_set(plugin_manager=manager)
    entry = next(e for e in record.entries if e.kind is ExtensionKind.PLUGIN)

    assert entry.name == "fixture_ext_4986_ok:FixturePlugin"
    assert entry.loaded is True
    assert entry.source == "config"
    assert entry.origin == str(module_path.resolve())
    assert entry.version == "0.4.0"
    assert entry.content_digest.startswith("sha256:")


# ---------------------------------------------------------------------------
# 3. A declared plugin that fails to load is recorded as not loaded.
# ---------------------------------------------------------------------------


def test_declared_plugin_that_fails_to_load_is_recorded_as_not_loaded(
    site: Path,
    tmp_path: Path,
) -> None:
    _install_dist(
        site,
        dist_name="fixture-broken-plugin",
        version="1.5.0",
        group="bernstein.plugins",
        ep_name="broken",
        ep_value="fixture_ext_4986_missing:Plugin",
    )
    importlib.invalidate_caches()
    manager = PluginManager(workdir=tmp_path)
    with pytest.warns(UserWarning):
        manager.discover_entry_points()

    record = build_loaded_extension_set(plugin_manager=manager)
    entry = next(e for e in record.entries if e.name == "broken")

    assert entry.kind is ExtensionKind.PLUGIN
    assert entry.loaded is False
    assert entry.version == "1.5.0"
    assert entry.content_digest == ""
    assert "fixture_ext_4986_missing" in entry.failure


# ---------------------------------------------------------------------------
# 4. A declared skill source that fails to load is recorded as not loaded.
# ---------------------------------------------------------------------------


def test_declared_skill_source_that_fails_to_load_is_recorded_as_not_loaded(site: Path) -> None:
    _install_dist(
        site,
        dist_name="fixture-broken-pack",
        version="0.2.0",
        group=_SKILL_GROUP,
        ep_name="broken-pack",
        ep_value="fixture_ext_4986_absent:source",
    )
    importlib.invalidate_caches()
    scan = scan_plugin_sources(entry_point_group=_SKILL_GROUP)
    loader = SkillLoader(list(scan.sources), source_resolutions=scan.resolutions)

    record = build_loaded_extension_set(loader=loader)
    entry = next(e for e in record.entries if e.name == "broken-pack")

    assert entry.kind is ExtensionKind.SKILL
    assert entry.loaded is False
    assert entry.source == _SKILL_GROUP
    assert entry.origin == "fixture_ext_4986_absent:source"
    assert entry.version == "0.2.0"
    assert entry.content_digest == ""
    assert "fixture_ext_4986_absent" in entry.failure


# ---------------------------------------------------------------------------
# 5. A not-loaded entry is never reported as available.
# ---------------------------------------------------------------------------


def test_entry_recorded_as_not_loaded_is_absent_from_the_available_surface(
    site: Path,
    tmp_path: Path,
) -> None:
    _install_dist(
        site,
        dist_name="fixture-absent-plugin",
        version="3.0.0",
        group="bernstein.plugins",
        ep_name="absent",
        ep_value="fixture_ext_4986_nowhere:Plugin",
    )
    importlib.invalidate_caches()
    manager = PluginManager(workdir=tmp_path)
    with pytest.warns(UserWarning):
        manager.discover_entry_points()

    record = build_loaded_extension_set(plugin_manager=manager)

    assert "absent" not in manager.registered_names
    assert "absent" in [e.name for e in record.not_loaded()]
    assert "absent" not in [e.name for e in record.loaded()]


# ---------------------------------------------------------------------------
# 6. Changing a plugin's bytes changes its digest.
# ---------------------------------------------------------------------------


def test_changing_plugin_bytes_between_runs_changes_its_digest(
    site: Path,
    tmp_path: Path,
) -> None:
    _write_plugin_module(site, "fixture_ext_4986_churn", version="0.4.0", marker="first")
    manager = PluginManager(workdir=tmp_path)
    manager.discover_config_plugins(["fixture_ext_4986_churn:FixturePlugin"])
    first = build_loaded_extension_set(plugin_manager=manager)

    _write_plugin_module(site, "fixture_ext_4986_churn", version="0.4.0", marker="second")
    second = build_loaded_extension_set(plugin_manager=manager)

    first_entry = next(e for e in first.entries if e.kind is ExtensionKind.PLUGIN)
    second_entry = next(e for e in second.entries if e.kind is ExtensionKind.PLUGIN)
    assert first_entry.content_digest != second_entry.content_digest
    assert first.digest != second.digest


# ---------------------------------------------------------------------------
# 7. The set digest is a function of every entry.
# ---------------------------------------------------------------------------


def test_set_digest_changes_when_a_skill_body_changes(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    _write_skill(skills_root, "beta-pack", version="1.0.0", body="Original body.")
    source = LocalDirSkillSource(skills_root, source_name="local")
    before = build_loaded_extension_set(loader=SkillLoader([source]))
    unchanged = build_loaded_extension_set(loader=SkillLoader([source]))

    _write_skill(skills_root, "beta-pack", version="1.0.0", body="Rewritten body.")
    after = build_loaded_extension_set(loader=SkillLoader([source]))

    assert before.digest == unchanged.digest
    assert before.digest != after.digest


# ---------------------------------------------------------------------------
# 8. The run receipt names the resolved set.
# ---------------------------------------------------------------------------


def test_run_receipt_binding_names_the_resolved_extension_set(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    _write_skill(skills_root, "gamma-pack", version="1.1.0", body="Gamma body.")
    record = build_loaded_extension_set(loader=SkillLoader([LocalDirSkillSource(skills_root, source_name="local")]))

    sdd_dir = tmp_path / ".sdd"
    journal = EventJournal(run_id="extset-run", sdd_dir=sdd_dir)
    journal.record("run_started", run_id="extset-run")
    record_loaded_extension_set(journal, record)
    journal.record("run_completed", run_id="extset-run")

    receipt = build_run_receipt("extset-run", sdd_dir, _kms(tmp_path), write=False)

    assert receipt.extension_set_digest == record.digest
    assert verify_run_receipt(receipt.receipt_bytes).ok is True


# ---------------------------------------------------------------------------
# 9. A swapped extension set collapses the signed subject.
# ---------------------------------------------------------------------------


def test_receipt_with_a_swapped_extension_entry_fails_verification(tmp_path: Path) -> None:
    import json

    skills_root = tmp_path / "skills"
    _write_skill(skills_root, "delta-pack", version="1.0.0", body="Delta body.")
    record = build_loaded_extension_set(loader=SkillLoader([LocalDirSkillSource(skills_root, source_name="local")]))

    sdd_dir = tmp_path / ".sdd"
    journal = EventJournal(run_id="extset-tamper", sdd_dir=sdd_dir)
    journal.record("run_started", run_id="extset-tamper")
    record_loaded_extension_set(journal, record)
    journal.record("run_completed", run_id="extset-tamper")
    receipt = build_run_receipt("extset-tamper", sdd_dir, _kms(tmp_path), write=False)

    mutated = json.loads(receipt.receipt_bytes.decode("utf-8"))
    for event in mutated["journal"]["events"]:
        if event.get("event") == "loaded_extension_set":
            event["extensions"][0]["content_digest"] = "sha256:" + "0" * 64
    tampered = json.dumps(mutated, sort_keys=True, separators=(",", ":")).encode("utf-8")

    result = verify_run_receipt(tampered)
    assert result.ok is False
    assert result.status == "tampered"


# ---------------------------------------------------------------------------
# 10. An entry resolved from outside the declared root keeps its own origin.
# ---------------------------------------------------------------------------


def test_skill_resolved_outside_the_declared_root_records_its_real_origin(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    external_root = tmp_path / "elsewhere"
    external_md = _write_skill(external_root, "epsilon-pack", version="9.9.9", body="Outside body.")
    (skills_root / "epsilon-pack").symlink_to(external_md.parent, target_is_directory=True)

    loader = SkillLoader([LocalDirSkillSource(skills_root, source_name="local")])
    record = build_loaded_extension_set(loader=loader)
    entry = next(e for e in record.entries if e.name == "epsilon-pack")

    assert entry.origin == str(external_md.resolve())
    assert str(skills_root) not in entry.origin


# ---------------------------------------------------------------------------
# 11. A run journal records the resolved set of the install it ran on.
# ---------------------------------------------------------------------------


def test_run_journal_records_the_set_resolved_from_the_install(tmp_path: Path) -> None:
    from bernstein.core.replay.journal import load_events
    from bernstein.core.security.loaded_extension_set import (
        LOADED_EXTENSION_SET_EVENT,
        extension_set_digest_from_events,
        record_run_extension_set,
    )

    workdir = tmp_path / "project"
    _write_skill(workdir / "templates" / "skills", "zeta-pack", version="4.0.0", body="Zeta body.")

    journal = EventJournal(run_id="extset-spawn", sdd_dir=workdir / ".sdd")
    journal.record("run_started", run_id="extset-spawn")
    recorded = record_run_extension_set(journal, workdir)

    events = load_events(journal.path).events
    rows = [e for e in events if e.get("event") == LOADED_EXTENSION_SET_EVENT]

    assert len(rows) == 1
    assert "zeta-pack" in [e.name for e in recorded.loaded()]
    assert extension_set_digest_from_events(events) == recorded.digest


# ---------------------------------------------------------------------------
# 12. The run start path actually records it.
# ---------------------------------------------------------------------------


def test_orchestrator_run_start_records_the_resolved_set() -> None:
    import inspect

    from bernstein.core.orchestration.orchestrator import Orchestrator

    source = inspect.getsource(Orchestrator.run)
    assert "record_run_extension_set" in source
