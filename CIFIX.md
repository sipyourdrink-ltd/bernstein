# CI fix for PR #7644 / issue #6976

Verdict: both full-head review findings are branch-caused and addressed locally.
Starting head: `6e921848296780ffcfa2b9be5cb5b8ddb5daab20`.

## Cause

The latest `LANE E LAND 7644 HELD` entry in `LANE-E-LOG.md` names the
independent `land-v2/review7644/REPORT.md` review. It records two findings:

- **F1: missing release note.** The scaffold repair changes user-visible
  behavior but adds only operator documentation. `CONTRIBUTING.md` and
  `docs/release-notes/README.md` require a release-note entry in the same PR.
  `scripts/rotate_release_notes.py:notes_gate_ok` accepts an edit to
  `docs/release-notes/unreleased.md` or a Markdown fragment under
  `docs/release-notes/fragments/`; operator docs and CIFIX.md do not count.
  Running `check-gate` against the original six changed paths reproduced
  **exit 1**. This is a missing branch deliverable, not a gate defect.
- **F2: nonportable fake CLI.** The new real-process test directly executed
  an extensionless Python file using a POSIX shebang and chmod. Windows
  `Popen(shell=False)` does not interpret that shebang. The concern is valid
  from the launch path; it was not reproduced on a Windows host here.
  The original template tests passed on macOS (**10 passed in 1.78s**),
  which does not disprove the Windows-specific defect.

## Change

- Add `docs/release-notes/fragments/6976-adapter-template-contract.md`,
  describing successful construction, current spawn arguments and the
  asynchronous result. It follows the documented `##` heading, short prose
  and issue-reference convention, also used by merged PR #6593's fragment.
- In `test_adapter_plugin_template.py`, use `sys.executable` as the fake CLI
  executable and put the fake Python program in the working directory as
  `run`, the template's first argument. Python opens that script on either
  platform. Remove the shebang/chmod dependency and expect only the remaining
  CLI arguments in the script's output. The test still launches a real child
  and checks live PID/handle return, stdout/stderr, addendum delivery and exit
  codes 0 and 7 for both default and custom rendered templates.

No adapter production code or gate logic needed changing.

## Test results

Local runtime: Python 3.14.3, macOS arm64. Commands used the existing venv
with `UV_NO_SYNC=1`, `UV_OFFLINE=1`, `PYTHONDONTWRITEBYTECODE=1`, and
`UV_CACHE_DIR`/`TMPDIR` under this checkout's `.cache/cifix-review/`.

```sh
uv run pytest -q -p no:cacheprovider --basetemp="$PWD/.cache/cifix-review/after" tests/unit/test_adapter_plugin_template.py tests/unit/test_adapter_plugin_sdk.py tests/unit/test_packaged_readme_renders_off_repo.py tests/unit/test_readme_l10n_cmd.py tests/unit/scripts/test_rotate_release_notes.py
```

**136 passed in 2.77s**, including both nested generated test suites.

The gate was rerun using the actual diff paths, with the new fragment staged:

```python
import subprocess
import sys

base = "0c94b12e38bc29ae681d0ea4a4cb2fc290b25cbd"
paths = subprocess.check_output(["git", "diff", "--name-only", base], text=True).splitlines()
sys.exit(subprocess.call([sys.executable, "scripts/rotate_release_notes.py", "check-gate", *paths]))
```

Run through `uv run python`: **Release-notes gate: OK, exit 0**.
No synthetic pathname or gate exemption was supplied.

- `uv run ruff check --no-cache tests/unit/test_adapter_plugin_template.py`:
  **exit 0**.
- `uv run ruff format --check --no-cache tests/unit/test_adapter_plugin_template.py`:
  **exit 0**, already formatted.
- `uv run bernstein readme-l10n verify --workdir .`: **exit 0**, all 23
  translations in sync.
- `git diff --check` and `git diff --cached --check`: **exit 0**.

Windows execution and hosted CI were not run; no Windows-host PASS is claimed.
The previous README/link/localization repair remains in starting head
`6e9218482`; its packaged-README and localization tests pass again above.

## Local handoff

This follow-up changes only the release fragment, the fake-CLI regression
test and CIFIX.md. Existing untracked CIFIX-OUT.txt, CIFIX-REVIEW.txt, ISSUE.md,
PR.md, PRODUCER.txt, REPORT.md and REVIEW.txt are preserved.
The checkout and `fix/6976-v4-blocker` branch remain for review by explicit
owner scope. No push, PR comment, label, remote CI rerun, merge or teardown
was performed; the new local commit still needs review before landing.
