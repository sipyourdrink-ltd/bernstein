## `post_merge` hooks now run after an agent's work is merged back

`LifecycleEvent.POST_MERGE` was declared, the notification bridge subscribed
to it, and `bernstein hooks` listed it, but the merge-back path never fired it.
A `post_merge` hook or notification sink could be configured and would never
run.

The merge-back now dispatches `post_merge` once a merge lands, on the
process-wide hook registry. The payload carries the merged branch
(`agent/<session>`), the branch it landed on, the commit `HEAD` points at after
the merge, and the session's task ids. A refused or failed merge fires
nothing. The merge has already happened when the event fires, so a hook that
raises or denies is logged and does not fail the merge (#5962).
