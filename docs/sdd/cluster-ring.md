# Cluster ring primitive

`core/protocols/cluster/shard_ring.py` contains the deterministic `HashRing`
and `ShardMap` value objects. It uses only the Python standard library and
does not perform network or registry operations. Tests live in
`tests/unit/test_shard_ring.py`. Production wiring belongs to a later slice.
