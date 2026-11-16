## Pi agents stream progress to the runner log

Pi now runs with `--print --mode json` and receives EOF on stdin, so it
streams task events to the runner log and exits after the prompt instead
of opening its interactive UI or waiting on an inherited stdin pipe.
Log growth supplies the existing liveness signal, preventing heartbeat
escalation from treating an actively streaming agent as idle before its
first artifact (#6943).
