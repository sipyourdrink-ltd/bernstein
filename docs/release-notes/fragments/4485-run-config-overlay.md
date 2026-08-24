## Run overrides no longer land in the repository's configuration

A run used to pin its overrides into the tracked `bernstein.yaml` in the work
tree, so any agent that committed with a staged tree carried the file and the
pull request proposed rewriting the repository's configuration for everyone.
Overrides now resolve from an untracked overlay inside the git directory
(`BERNSTEIN_CONFIG_OVERLAY` points it elsewhere, `BERNSTEIN_CONFIG_OVERRIDE`
carries an inline mapping), merged over the committed file at load time in the
order committed file < overlay < explicit override. The committed file is read
by a run and never written by one. `.claude/mcp.json` still has to exist in the
work tree because Claude Code reads it from a fixed path, so it is registered
in the repository's local git excludes instead. A new required quality gate,
`run_config`, refuses any change whose diff touches a run-configuration path
and names the file (#4485).
