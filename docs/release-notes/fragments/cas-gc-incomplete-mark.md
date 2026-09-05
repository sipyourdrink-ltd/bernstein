## CAS GC refuses to sweep on a partial mark

`bernstein gc cas` is a mark-and-sweep collector: it walks the durable roots for referenced digests, then deletes unreferenced blobs older than the retention window. The mark phase caught every scanner failure, logged it, and carried on with the digests it already had. A root that failed to open contributed nothing, so a blob whose only reference lived there was indistinguishable from an unreferenced one and was deleted, with nothing in the result telling the operator a root had been skipped.

The mark phase now reports which roots it could not read, and the sweep deletes nothing when that list is non-empty: the counts still describe what a complete mark would have considered, the way they do for a dry run, and `bernstein gc cas` exits non-zero naming the roots that failed.

Two supporting changes. The lineage scanner no longer raises `AttributeError` on a spine line that is not an object or whose `content_hash` is not a string, which was the realistic way for an entire root to drop out of the mark. And the prune receipt's `root_set_hash` carries the digest of the root set the sweep was decided against, instead of the `null` it has held since the field was added; receipts are now version 2, and one is only written for a sweep whose mark phase read every root.
