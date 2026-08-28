with open("src/bernstein/core/tasks/task_lifecycle.py", "r") as f:
    content = f.read()

import re
match = re.search(r'for _t in batch:.*?def ', content, re.DOTALL)
if match:
    print(match.group(0)[:1500])
