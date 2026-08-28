## The volunteer hub refuses to boot with more than one worker

`bernstein volunteer hub` used to print a note asking the operator not to run
it with `uvicorn --workers N>1`, then start anyway. `LeaseStore` serialises
writes with an in-process `asyncio.Lock` and appends to JSONL without
`fcntl.flock`, so a second worker interleaves partial lines and hands one task
to two claimants — a corruption that only surfaces later, as a duplicate
submission, with nothing in the log pointing back at `WEB_CONCURRENCY`. The
note is now a refusal that fires before the port is bound, and the deployment
README documents the single-process constraint alongside the compose file.
