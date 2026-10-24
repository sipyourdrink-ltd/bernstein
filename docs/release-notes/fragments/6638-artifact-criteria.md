## Artifact contracts enforce their declared criteria

Artifact completion evaluates criteria declared in `artifact_spec` alongside
artifact completion signals, once per identical type/value pair. A failing
criterion prevents the signed success receipt; filesystem gates remain in
force (#6638).
