## The audit tail digest is pinned against a line truncated mid-write

3.21.0 covered `_hmac_chain_tail_digest` skipping non-dict and unparseable
audit `.jsonl` lines. New tests add the shape a process that died mid-append
actually leaves behind - a line truncated mid-object - with a valid `hmac`
entry on either side of it, and assert the scan neither raises nor stops early:
the later valid entry still wins (#5950).
