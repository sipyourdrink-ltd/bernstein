import re
with open("src/bernstein/core/skills/lifecycle.py", "r") as f:
    text = f.read()

# 1. Add pack: str | None = None to LockEntry
text = re.sub(
    r'(class LockEntry:\n.*?\n.*?\n.*?name: str\n.*?source: str\n.*?path: str\n.*?digest: str\n)',
    r'\1    pack: str | None = None\n',
    text,
    flags=re.DOTALL
)

# 2. In _read_lock
old_read_lock = """        if (
            isinstance(name, str)
            and isinstance(source, str)
            and isinstance(path_value, str)
            and isinstance(digest, str)
        ):
            out[name] = LockEntry(name=name, source=source, path=path_value, digest=digest)"""

new_read_lock = """        pack = item_dict.get("pack")
        if (
            isinstance(name, str)
            and isinstance(source, str)
            and isinstance(path_value, str)
            and isinstance(digest, str)
            and (pack is None or isinstance(pack, str))
        ):
            out[name] = LockEntry(name=name, source=source, path=path_value, digest=digest, pack=pack)"""
text = text.replace(old_read_lock, new_read_lock)

# 3. In _write_lock
old_write_lock = """                f"path = {_toml_quote(entry.path)}",
                f"digest = {_toml_quote(entry.digest)}",
            )
        )"""

new_write_lock = """                f"path = {_toml_quote(entry.path)}",
                f"digest = {_toml_quote(entry.digest)}",
            )
        )
        if entry.pack is not None:
            lines.append(f"pack = {_toml_quote(entry.pack)}")"""
text = text.replace(old_write_lock, new_write_lock)

# 4. In install_plugin_local, check for pack-over-pack collision and update record_lock
old_plugin_guard = """            prior = existing_lock.get(name)
            if prior is not None and prior.source != _PLUGIN_LOCK_SOURCE:
                raise SkillLifecycleError(
                    f"{name}: already installed from source {prior.source!r}; "
                    f"installing this plugin would replace it. Re-run with --force to take the plugin's copy"
                )"""

new_plugin_guard = """            prior = existing_lock.get(name)
            if prior is not None:
                if prior.source != _PLUGIN_LOCK_SOURCE:
                    raise SkillLifecycleError(
                        f"{name}: already installed from source {prior.source!r}; "
                        f"installing this plugin would replace it. Re-run with --force to take the plugin's copy"
                    )
                elif prior.pack is not None and prior.pack != name_field:
                    raise SkillLifecycleError(
                        f"{name}: already installed from plugin pack {prior.pack!r}; "
                        f"installing pack {name_field!r} would replace it. Re-run with --force to take the new plugin's copy"
                    )"""
text = text.replace(old_plugin_guard, new_plugin_guard)

old_plugin_record = """    for result in installed:
        entries[result.name] = LockEntry(
            name=result.name,
            source=_PLUGIN_LOCK_SOURCE,
            path=str(skills_dir / result.name),
            digest=result.digest.digest,
        )"""

new_plugin_record = """    for result in installed:
        entries[result.name] = LockEntry(
            name=result.name,
            source=_PLUGIN_LOCK_SOURCE,
            path=str(skills_dir / result.name),
            digest=result.digest.digest,
            pack=str(name_field) if name_field else None,
        )"""
text = text.replace(old_plugin_record, new_plugin_record)

with open("src/bernstein/core/skills/lifecycle.py", "w") as f:
    f.write(text)
print("patched lifecycle.py")
