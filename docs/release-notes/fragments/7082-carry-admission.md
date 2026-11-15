## Restrict privileged jobs on carry branches

Privileged workflow jobs reject carry refs at job admission while retaining
their existing conditions and read-only build checks (#7082).
