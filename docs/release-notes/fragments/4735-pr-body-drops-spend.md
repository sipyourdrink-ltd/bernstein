## Pull-request descriptions no longer carry what the run spent

`build_pr_body` opened every description with the session's spend
(`> 📝 4 files · +507 / -0 · $38.94`) and closed it with a `## Cost` section
listing the total, the token count, an effective rate per million tokens and a
per-role breakdown. All of it landed on a public page.

The module already refuses to source a description from the session's own
status text, on the grounds that it describes the run while a reader of the
pull request is asking about the diff. Spend is the same kind of fact and was
the one place the rule was not applied. Both the headline figure and the
section are gone; the run's cost accounting is unchanged and still available
where it belongs, in the run report and the retrospective.
