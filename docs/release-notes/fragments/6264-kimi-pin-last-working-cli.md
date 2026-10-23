## The kimi adapter installs kimi-cli 1.51.0, the last release that runs

kimi-cli is archived upstream. Its final release, 1.52.0, routes every
invocation through a deprecation gate. `kimi --yolo -c <prompt>` prints a
one-line notice and exits 0, and `kimi` with no arguments runs the Kimi Code
install script. A kimi worker on 1.52.0 therefore did nothing and reported
success, and the nightly conformance canary skipped kimi on every run.

The adapter contract, the adapter's install hint and `ADAPTER_GUIDE.md` now
pin `kimi-cli==1.51.0`. Driving Moonshot's successor, Kimi Code CLI, needs its
own adapter work (#6264).
