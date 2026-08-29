## A sealed run-graph receipt can be checked against the tree it sealed

`verify_run_graph_receipt` rebuilds a fan-out's graph from the worktrees and walks every branch's lineage spine, then compares the result with the sealed receipt rather than replaying the receipt's own stored hashes. An edited receipt, a branch whose journal was altered, a missing branch, and a receipt with no spine anchor are each refused by name (#3760).
