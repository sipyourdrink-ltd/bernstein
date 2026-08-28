with open("tests/integration/orchestration/test_deadlock_detection.py", "r") as f:
    lines = f.readlines()

new_lines = []
in_with = False
for line in lines:
    if line.strip() == "with tempfile.TemporaryDirectory() as tmpdir:":
        in_with = True
        new_lines.append(line)
        continue
    if in_with and not line.startswith("    ") and not line.startswith("import") and not line.startswith("@") and not line.startswith("async def"):
        # We only want to indent lines that are supposed to be inside the with block.
        # Actually, let's just rewrite the whole test again.
        pass

