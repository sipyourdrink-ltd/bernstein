## Recover read paths nested under tool call args and protocol frames

`derive_read_paths` previously scanned only top-level `path` and `file_path`
fields in journal rows. Real tool calls (such as `fs.read`) and protocol frames
nest their path arguments under `args` or `frame`. The derivation now descends
into these known payload carriers to extract all accessed paths, ensuring
the read-set merge gate accurately reflects files accessed during runs (#5646).
