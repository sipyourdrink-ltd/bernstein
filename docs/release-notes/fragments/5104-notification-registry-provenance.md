## Notification registry conflicts identify both modules

Duplicate notification driver kinds and live sink IDs now use the shared
registration guard. Errors name both the original and incoming modules,
including drivers loaded from built-ins or entry points, while preserving
the original registration. Unregistering a sink permits its ID to be reused.
