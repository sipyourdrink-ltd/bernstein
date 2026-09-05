## A routine's cooldown resets only on a run that found something

A scheduled routine that ran, found nothing, and recorded that fact used
to suppress itself for the whole cooldown window — so "nothing is
happening" and "nothing is checking" looked identical in the fire log.
`TriggerFireRecord` now carries `produced`, and only a productive fire
moves the cooldown clock. An empty run is still recorded, so
`get_fire_history` shows it; it just does not start the timer. Records
written before this change have no `produced` field and are read as
productive, which is what they were.

## One action per event id, decided atomically

The dedup check read an in-memory cache and the write rewrote the file
afterwards, so two ticks — or two processes under the schedule supervisor
— could both pass the check on the same event id and both proceed.
`TriggerManager.claim_dedup` makes the check and the write one section
against every other thread and process, under a `flock` on a lock file
beside the cache, re-reading the cache from disk inside the lock. The
cache is now written through a scratch sibling too, so an interrupted
write no longer releases every reservation at once (#5113).
