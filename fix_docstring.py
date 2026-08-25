import re
with open("src/bernstein/core/skills/lifecycle.py", "r") as f:
    text = f.read()

old_docstring = """    A skill whose name already holds a lock row from a *different* source -
    ``bernstein-skills.toml`` - is refused the same way,
    naming the source it would have replaced. Overwriting it would delete an
    install the operator chose deliberately and silently flip that row's
    provenance to ``"plugin"``, letting a pack shadow a trusted skill. Pass
    ``force=True`` to take the replacement anyway. A same-source reinstall
    (drift heal, upgrade) is untouched and stays silent."""

new_docstring = """    A skill whose name already holds a lock row from a *different* source -
    such as ``bernstein-skills.toml`` or a different plugin pack - is refused
    the same way, naming the source or pack it would have replaced. Overwriting it
    would delete an install the operator chose deliberately and silently flip that
    row's provenance, letting a pack shadow a trusted skill. Pass ``force=True`` to
    take the replacement anyway. A same-source reinstall (drift heal, upgrade) of
    the same pack is untouched and stays silent."""

text = text.replace(old_docstring, new_docstring)

with open("src/bernstein/core/skills/lifecycle.py", "w") as f:
    f.write(text)
print("fixed docstring")
