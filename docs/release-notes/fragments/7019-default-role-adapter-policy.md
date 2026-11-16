### Added

- Support an optional `*` adapter allow-list for roles without a named policy.
  An empty default denies every adapter; existing named-role behavior and
  policies without a default retain their semantics (#7019).
