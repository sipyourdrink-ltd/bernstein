## A failed workflow node now stops every later layer

`bernstein workflow run` (and `WorkflowRunner.run`) used to keep
dispatching nodes in later layers after a node failed, whenever those
nodes depended only on branches that had passed. Those nodes ran their
commands or agents, wrote `success` checkpoints, and appeared twice in the
result. A failure now skips every later layer once, without dispatching
anything, matching `bernstein workflow resume`.
