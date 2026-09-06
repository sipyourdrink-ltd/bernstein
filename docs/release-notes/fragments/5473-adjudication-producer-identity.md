## Adjudication producer identity and independence classification

Adjudication records now explicitly record the producing identity (`produced_by`) of the agent or model whose work was judged, alongside its independence classification (`adjudication_class`: `independent`, `weak`, `unattributed`, `unresolved`). Records bind producer identities into the canonical JCS byte representation while maintaining additive backward compatibility for historical records without a producer. Cross-model reviewer selection rejects model overrides that collide with the authoring model (#5473).
