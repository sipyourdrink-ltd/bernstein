FIXED: 1 of 1 blocking findings

F1: No docs/release-notes/fragments/5348-*.md despite a user-visible behaviour change
  Status: FIXED
  What changed: Added docs/release-notes/fragments/5348-fallback-uses-registry-key.md (commit da324a629)
  Format matches peer examples (5337, 5082): `## title` + paragraph ending `(#5348)`
  Both user-visible axes documented: (1) new WARNING for unregistered adapters,
  (2) changed resolved value for registered adapters (registry key vs display name)
