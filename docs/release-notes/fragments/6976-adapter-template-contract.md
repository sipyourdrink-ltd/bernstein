## Generated adapters match the current CLIAdapter contract

Adapters created from the cookiecutter template now construct successfully
and accept the current spawn arguments. They launch asynchronously and return
the live process handle, PID and log path, with timeout watchdog support.
Generated tests cover the current contract instead of removed result fields
(#6976).
