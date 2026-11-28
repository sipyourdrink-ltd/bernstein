## `config-path` reports the seed file a run would load

With `BERNSTEIN_SEED_PATH` naming another file, `bernstein config-path`
reported `bernstein.yaml` while the seed resolver selected the override. The
command now reports the override when it names an existing file, through the
same resolver (#7958).
