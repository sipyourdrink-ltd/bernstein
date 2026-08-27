- `bernstein pr` now reports when git could not describe the branch instead of
  writing a description that says the branch changed nothing. Provenance is
  built only from a diff that exists, so a pull request never carries a digest
  of the empty string beside a command to verify it. Descriptions also open
  with a one-line summary of size, gates and cost, list files as a table, and
  fold the diff-stat away.
