def spawn(self, **kwargs):
    explicit_max_turns = self._extra_spawn_kwargs.get('explicit_max_turns')
    if explicit_max_turns is not None:
        kwargs['explicit_max_turns'] = explicit_max_turns
    return self._adapter.spawn(**kwargs)