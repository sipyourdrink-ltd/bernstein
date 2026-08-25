import re
with open("tests/unit/skills/test_plugin_cross_source_collision.py", "r") as f:
    text = f.read()

# Change the `LockEntry` import usage if needed in tests:
# Actually we don't need to change `workdir_with_toml_alpha`, since pack is optional.

old_allowed_test = """def test_collision_with_another_plugin_source_is_allowed(plugin_dir: Path, tmp_path: Path) -> None:
    \"\"\"Pack-over-pack keeps today's behaviour; only cross-*source* is refused.

    Both rows carry ``source="plugin"``, so this is the same-source path. The
    issue scopes namespacing skills by *pack* out, so a second pack shadowing
    the first stays as it was rather than being half-fixed here.
    \"\"\"
    workdir = tmp_path / "project"
    install_plugin_local(plugin_dir, scope=InstallScope.PROJECT, workdir=workdir)

    other = tmp_path / "other-pack"
    (other / "skills").mkdir(parents=True)
    (other / "plugin.json").write_text(
        json.dumps({"name": "other-pack", "version": "1.0.0", "skills": "./skills/"}),
        encoding="utf-8",
    )
    _write_skill(other / "skills" / "alpha" / "SKILL.md", "alpha", body="From other-pack.")

    result = install_plugin_local(other, scope=InstallScope.PROJECT, workdir=workdir)

    assert [r.name for r in result.installed] == ["alpha"]
    assert result.skipped == []"""

new_test = """def test_collision_with_another_plugin_pack_is_refused(plugin_dir: Path, tmp_path: Path) -> None:
    \"\"\"Pack-over-pack collision is now refused because they have different pack names.\"\"\"
    workdir = tmp_path / "project"
    install_plugin_local(plugin_dir, scope=InstallScope.PROJECT, workdir=workdir)

    other = tmp_path / "other-pack"
    (other / "skills").mkdir(parents=True)
    (other / "plugin.json").write_text(
        json.dumps({"name": "other-pack", "version": "1.0.0", "skills": "./skills/"}),
        encoding="utf-8",
    )
    _write_skill(other / "skills" / "alpha" / "SKILL.md", "alpha", body="From other-pack.")
    _write_skill(other / "skills" / "gamma" / "SKILL.md", "gamma", body="From other-pack.")

    result = install_plugin_local(other, scope=InstallScope.PROJECT, workdir=workdir)

    assert [r.name for r in result.installed] == ["gamma"]
    assert [s.name for s in result.skipped] == ["alpha"]
    assert "'my-pack'" in result.skipped[0].reason
    assert "would replace it. Re-run with --force to take the new plugin's copy" in result.skipped[0].reason

def test_collision_with_another_plugin_pack_is_allowed_with_force(plugin_dir: Path, tmp_path: Path) -> None:
    workdir = tmp_path / "project"
    install_plugin_local(plugin_dir, scope=InstallScope.PROJECT, workdir=workdir)

    other = tmp_path / "other-pack"
    (other / "skills").mkdir(parents=True)
    (other / "plugin.json").write_text(
        json.dumps({"name": "other-pack", "version": "1.0.0", "skills": "./skills/"}),
        encoding="utf-8",
    )
    _write_skill(other / "skills" / "alpha" / "SKILL.md", "alpha", body="From other-pack.")

    result = install_plugin_local(other, scope=InstallScope.PROJECT, workdir=workdir, force=True)

    assert [r.name for r in result.installed] == ["alpha"]
    assert result.skipped == []
    
    entries = _read_lock(workdir / SKILLS_LOCK_FILENAME)
    assert entries["alpha"].pack == "other-pack"
"""
text = text.replace(old_allowed_test, new_test)

with open("tests/unit/skills/test_plugin_cross_source_collision.py", "w") as f:
    f.write(text)
print("patched test file")
