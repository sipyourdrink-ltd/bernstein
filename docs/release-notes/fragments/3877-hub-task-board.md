## A volunteer hub can offer work with no git forge behind it

Every task a donor could lease came from a git forge: the lease store took
whatever task id its caller supplied, and the runner's task shape carries an
issue number. A project with no public issue tracker, or one that does not want
its volunteer queue to be its issue tracker, had no way to offer work at all.

A hub now keeps its own task board. `POST /volunteer/tasks` publishes an offer
under the `volunteer:publish` scope, `GET /volunteer/tasks` lists the board to a
caller holding `volunteer:claim`, and the donor claims, heartbeats and submits
through the endpoints it already used. The board is an append-only log next to
the lease log, so a hub torn down and brought back still offers what it offered.

A hub-native task id is `hub:` followed by the sha256 of the task's content.
The prefix is reserved, so an id mirrored from a git forge can never collide
with a board one; the digest lets a donor recompute the id from the content it
was handed; and republishing an identical offer lands on the id that already
exists instead of doubling the board. An id under that prefix that the board
does not carry cannot be leased, and the board's declared size — not the size
the claimant asks with — is what donor admission is decided on.
