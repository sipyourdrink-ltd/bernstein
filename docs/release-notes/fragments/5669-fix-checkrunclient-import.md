## Fix dangling TYPE_CHECKING import for CheckRunClient

`src/bernstein/core/volunteer/verification_check_run.py` imported `CheckRunClient`
from nonexistent `bernstein.core.github_app.check_runs`. The import now points
to the actual module path `bernstein.github_app.check_runs` (#5669).
