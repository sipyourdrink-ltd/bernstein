with open("src/bernstein/core/tasks/task_lifecycle.py", "r") as f:
    content = f.read()

import re
match = re.search(r'def claim_and_spawn_batches.*?def ', content, re.DOTALL)
if match:
    print(match.group(0)[:1000])
    print("...")
    print(match.group(0)[-1000:])
