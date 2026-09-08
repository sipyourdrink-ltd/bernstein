## Fix out-of-tree POSIX path assertion in test_read_paths on Windows

`derive_read_paths` returns `ReadPathSet.out_of_tree` with POSIX forward-slash
separators. `test_out_of_tree_path_lands_in_separate_set` in
`tests/unit/core/replay/test_read_paths.py` compared against `str(Path)`, which
uses backslash separators on Windows. The test now compares against the POSIX
form (`outside.as_posix()`), allowing the suite to pass across all platforms (#5665).
