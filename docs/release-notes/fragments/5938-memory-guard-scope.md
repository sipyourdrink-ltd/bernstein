## Keep the pytest controller outside the memory cap

The test suite applies its address-space guard to test workers and plain
runs, leaving the pytest-xdist controller uncapped. Resource failures and
the reported worker-crash controller abort include guard context. Diagnostic
regressions run without the POSIX-only probe on Windows and preserve custom
caps and inherited Python module paths in child sessions.
