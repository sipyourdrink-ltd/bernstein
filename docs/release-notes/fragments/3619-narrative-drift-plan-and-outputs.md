## Narrative getting-started pages corrected against the current CLI

The tutorial's sample `bernstein init`, `bernstein doctor`, `bernstein status`,
`bernstein recap`, and `bernstein cost` outputs did not match what the
commands render today, and its sample plan used a `scope` list plus
`complexity: simple` that the plan loader rejects (`scope` is one of
`small|medium|large`, `complexity` is one of `low|medium|high`, and owned files
go in `files`). The remote quickstart also documented `bernstein run -g`,
which is not a registered command (`-g` belongs to the top-level command).
Each claim was verified by running the command against the current CLI before
the page was corrected (#3619).
