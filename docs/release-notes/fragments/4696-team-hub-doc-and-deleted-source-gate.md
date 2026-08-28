- The team-hub concept page documented a loader module that had been removed
  as unreachable code, so its copy-pasteable example imported something that
  no longer existed and its manifest example omitted a required field. The
  page now documents the manifest parser that ships, and both examples are
  exercised. A change that deletes a file the drift playbook names as a doc's
  source of truth now fails on the pull request that deletes it, rather than
  on the push run afterwards with the broken page already published.
