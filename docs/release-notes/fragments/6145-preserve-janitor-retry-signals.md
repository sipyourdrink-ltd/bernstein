## Janitor retries retain the original completion checks

Fix tasks created after verification failures or LLM judge retries now carry the
original completion signals, owned files, and dependencies. This lets each
attempt use the same definition of done, avoiding repeated work caused by
missing verification criteria (#6145).
