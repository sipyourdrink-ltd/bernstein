## Preserve Python encoding settings across filtered Windows spawns

Filtered agent and worker subprocesses now preserve operator-set `PYTHONUTF8`
and `PYTHONIOENCODING` values, so Windows Python processes do not lose their
configured UTF-8 or standard-stream encoding at the environment-isolation
boundary. Bernstein passes the existing values through unchanged and does not
set either variable when the operator has not configured it (#5718).
