import re
with open("src/bernstein/core/skills/lifecycle.py", "r") as f:
    text = f.read()

# Fix long line
old_guard_long = """                    raise SkillLifecycleError(
                        f"{name}: already installed from plugin pack {prior.pack!r}; "
                        f"installing pack {name_field!r} would replace it. Re-run with --force to take the new plugin's copy"
                    )"""

new_guard_long = """                    raise SkillLifecycleError(
                        f"{name}: already installed from plugin pack {prior.pack!r}; "
                        f"installing pack {name_field!r} would replace it. "
                        f"Re-run with --force to take the new plugin's copy"
                    )"""
text = text.replace(old_guard_long, new_guard_long)

# Remove debug print
old_print = """            print(f"PRIOR FOR {name}: {prior}")
            parse_skill_md(skill_md)"""

new_print = """            parse_skill_md(skill_md)"""
text = text.replace(old_print, new_print)

with open("src/bernstein/core/skills/lifecycle.py", "w") as f:
    f.write(text)
