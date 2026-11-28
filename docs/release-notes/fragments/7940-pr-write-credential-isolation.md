## PR write-credential isolation and autosync token move

PR observability comments and pre-merge autosync pushes now run in a
default-branch `workflow_run` publisher (`.github/workflows/pr-publish.yml`)
instead of inside read-only `pull_request` workflows. **Operator action
required before enabling the publisher:** move `BERNSTEIN_AUTOSYNC_TOKEN`
from repository secrets to the `automation` environment so same-repo PR
workflows cannot read the write credential while jobs bound to
`environment: automation` retain push access.
