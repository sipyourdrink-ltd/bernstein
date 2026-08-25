import re
with open("src/bernstein/core/skills/lifecycle.py", "r") as f:
    text = f.read()

old_write_lock = """                f"digest = {_toml_quote(entry.digest)}",
                "",
            )
        )"""

new_write_lock = """                f"digest = {_toml_quote(entry.digest)}",
            )
        )
        if entry.pack is not None:
            lines.append(f"pack = {_toml_quote(entry.pack)}")
        lines.append("")
"""
text = text.replace(old_write_lock, new_write_lock)

with open("src/bernstein/core/skills/lifecycle.py", "w") as f:
    f.write(text)
print("patched write_lock")
