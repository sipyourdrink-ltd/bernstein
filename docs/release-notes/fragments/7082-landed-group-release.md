## Admit queue releases only after the exact tested group lands

The post-CI dispatcher requires the tested queue-group SHA in main history
and rejects carry tails before invoking privileged release jobs (#7082).
