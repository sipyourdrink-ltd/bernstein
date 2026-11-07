# Declaration quarantine store

`core/security/declaration_quarantine.py` owns one-shot initialization-failure
markers for declarations. Its identity hashes the declaration kind, source,
origin and optional skill name; origins keep duplicate skill names distinct.
The shared kind key allows later adapter/plugin/routine integrations without
changing the threshold-based task quarantine store.

`default_loader_from_templates` supplies a workspace store to `SkillLoader`.
Directly constructed, isolated loaders can omit the store and retain the
in-memory quarantine and callback contract. Persistent loaders check markers
before enumerating a source or registering an artifact. Skips preserve the
original record without calling the failure callback again.

The store writes atomic JSON under the audit chain's cross-process transaction
lock, preventing concurrent read/modify/write updates from losing markers or
duplicating failure events. Quarantine writes the blocking marker before its
audit event; release journals the actor before removing the marker. An I/O
failure can therefore leave a conservative held marker; the two files are not
a single crash-atomic transaction. Corrupt state is never silently cleared.

Doctor and the `doctor:declaration-quarantine` audit adapter use one read-only
summary producer. Status reads the same marker file. Re-enable uses the existing
quarantine CLI group; it is not an automatic retry mechanism.
