## Sharded pull-request test lanes compare against a pinned base commit

Each shard of the pull-request test lane resolved its own comparison base from
the base branch name and re-ran the impacted-test selector itself. `--shard i/N`
partitions whatever list it is handed, so the shards only cover the whole
affected set when every shard computed the same list, and nothing enforced
that. Runner-slot contention routinely staggers the shards of one run by hours;
when the base branch advanced in between, the late shard compared against a
different commit, selected a different set, and the slices stopped covering
their union -- some files ran twice while others ran in no shard at all, with
every shard still reporting success. A test importing a changed module could
therefore be skipped by the pull-request lane and only fail later in the
merge-queue lane, which runs the suite in full.

Both sharded lanes now fetch the base commit named on the event payload and
compare against that sha. The payload is fixed for the run, so every shard --
whatever time it starts, and in any re-run attempt -- selects from one list and
the slices are a partition of it again. Because the pinned sha is the commit the
pull-request merge was computed against, the selection is also the change's own
diff rather than the accumulated difference between two branch tips, so the
lane selects a smaller and more accurate set of tests.
