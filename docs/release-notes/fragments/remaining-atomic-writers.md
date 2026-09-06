## Seven more atomic writers go through the crash-safe path

Follow-up to the trigger-state and fleet-context conversion. Seven further write helpers renamed a temporary into place without ever calling `fsync`, so the rename could be durable while the bytes behind it were not: the MCP catalog user config, the review-responder dedup state, the tunnel registry, the plugin pin manifest, the evolution upgrade applicator, and both writers in template compression, the one that emits compressed role templates and `store_backup`, which keeps the originals.

`store_backup` is worth calling out because it verifies its own write by reading the file back and comparing a SHA-256. Without an `fsync` that readback came from the page cache, so it proved the write call had run rather than that a backup existed on disk to be read after a crash.

Two helpers wrote text with no encoding, following the host locale: the MCP catalog config through `Path.write_text` and the applicator through `Path.open("w")`. Both now name UTF-8, and so do their matching read paths, which is the half that makes it safe: hard-coding UTF-8 on the write side alone would turn a self-consistent pair into a mismatched one, silently corrupting a catalog on a cp1252 host and raising on a cp932 one.

The canonical helper applies owner-only permissions to what it writes, which is right for `.sdd/runtime/` state and narrows four of these files from `0644`. Role prompt templates are the exception and opt back out to `0644`, because their directory can be the one shipped inside the installed package, where an install written as one account and run as another must not lose read access to its own templates (#5513).
