import re
with open("src/bernstein/core/skills/lifecycle.py", "r") as f:
    text = f.read()

# Update signature of _record_plugin_lock
old_sig = """def _record_plugin_lock(
    installed: list[InstallResult],
    skills_dir: Path,
    *,
    workdir: Path,
) -> None:"""

new_sig = """def _record_plugin_lock(
    installed: list[InstallResult],
    skills_dir: Path,
    *,
    workdir: Path,
    pack: str | None = None,
) -> None:"""
text = text.replace(old_sig, new_sig)

# Update the call in install_plugin_local
old_call = "        _record_plugin_lock(installed, skills_dir, workdir=workdir)"
new_call = "        _record_plugin_lock(installed, skills_dir, workdir=workdir, pack=str(name_field) if name_field else None)"
text = text.replace(old_call, new_call)

# Update inside _record_plugin_lock
old_ins = """    for result in installed:
        entries[result.name] = LockEntry(
            name=result.name,
            source=_PLUGIN_LOCK_SOURCE,
            path=str(skills_dir / result.name),
            digest=result.digest.digest,
            pack=str(name_field) if name_field else None,
        )"""

new_ins = """    for result in installed:
        entries[result.name] = LockEntry(
            name=result.name,
            source=_PLUGIN_LOCK_SOURCE,
            path=str(skills_dir / result.name),
            digest=result.digest.digest,
            pack=pack,
        )"""
text = text.replace(old_ins, new_ins)

with open("src/bernstein/core/skills/lifecycle.py", "w") as f:
    f.write(text)
print("patched 2")
