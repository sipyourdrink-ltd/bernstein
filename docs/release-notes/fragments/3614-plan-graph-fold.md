## Derive the recorded structural plan graph offline

A pure journal fold now reconstructs structural task nodes using the producer's
canonical graph encoding. Missing latest snapshots return an explicit
unavailable result, and malformed graph evidence fails closed. Generated
producer-prefix tests cover independent append failures and determinism across
processes. Scheduler integration and amendments remain deferred (#3614).
