## ASI detectors read the payload shapes they are handed

Three detectors in the OWASP ASI pack returned a pass on payloads they had not read. ASI02 and ASI05 opened with `isinstance(tool_args, dict)`, so a tool call carrying its arguments as a positional list or a bare string was never scanned. ASI01 excluded `bytes` from its haystack collector, so a prompt sent down a binary channel was dropped. ASI04 gated on `isinstance(components, Iterable)`, which a string satisfies by yielding characters and a dict by yielding keys, so neither produced any component to check and the detector reported clean.

Tool arguments now render through one collector that reads mappings, sequences, scalars and binary values alike. `loaded_components` accepts a mapping of name to record as well as a list, and a value that is not a component manifest at all is reported rather than passed, since nothing in it was checked for a signature.
