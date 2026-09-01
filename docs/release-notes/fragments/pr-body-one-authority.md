## Every number in a pull-request description now describes the same diff

`bernstein pr` composed one body from two sources. The commit list and the
provenance hash were asked of `git diff <base>..<branch>`; the "Full diff-stat"
block was taken from the run's wrap-up file, which records what was in the
agent's worktree when it finished. When a later stage removed the run's own
config edit from the commit, nothing rewrote that snapshot, so descriptions
shipped a diff-stat naming a file the branch does not contain — unreconcilable
with the Files tab, and a config change published in a public description after
the diff had been cleaned of it. The diff-stat is now asked of the branch like
the rest, and falls back to the recorded value only when git cannot answer.

The opening line counted per-commit churn, so a file added and removed on the
same branch was still counted and its lines counted twice: one body opened
"7 files · +534 / -41" over a diff GitHub rendered as 6 files, +498 / -3. It now
states the net diff, with the commit sum kept as the fallback when git cannot
answer.
