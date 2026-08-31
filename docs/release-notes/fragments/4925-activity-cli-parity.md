## Activity CLI parity for research, data, and ops modalities

The ``bernstein activity`` group now ships ``research``, ``data``, and ``ops``
subgroups, each with ``run`` and ``verify`` commands, so every typed activity
boundary crossing is reachable from the CLI -- not just browser activities
(#4925). Each ``run`` anchors its result into the run journal as an
``activity.result`` entry, and each ``verify`` replays the anchored chain
offline, recomputing verdicts from the content store.