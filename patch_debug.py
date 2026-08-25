import re
with open("src/bernstein/core/skills/lifecycle.py", "r") as f:
    text = f.read()

text = text.replace("parse_skill_md(skill_md)", "print(f'PRIOR FOR {name}: {prior}'); parse_skill_md(skill_md)")

with open("src/bernstein/core/skills/lifecycle.py", "w") as f:
    f.write(text)
print("patched debug")
