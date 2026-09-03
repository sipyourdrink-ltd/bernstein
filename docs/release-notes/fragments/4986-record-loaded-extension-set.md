## Record the skill and plugin set a run actually loaded

Every agent run now records the resolved skill and plugin set — the entries
resolution produced — in the run journal, and binds a digest of that set into
the signed run receipt.  Operators can answer which extensions the run
actually loaded, not just which ones the install declared: each entry carries
its resolved source, version, origin path, and a SHA-256 digest of the
loaded bytes.  Declared-but-unloaded entries are present with ``loaded=False``
and the error text, so a resolution failure is distinguishable from a missing
declaration.  The digest is recomputed from the embedded journal rows on both
the ``build_run_receipt`` and ``verify_run_receipt`` paths, so the receipt
names a value that is fully derivable offline.  The new ``loaded_extension_set``
event type and the ``extension_set_digest`` binding block field are visible in
``bernstein replay`` output and in ``bernstein verify receipt`` output.

(#4986)
