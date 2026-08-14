def spawn(self, **kwargs):
    if 'explicit_max_turns' in kwargs:
        wrapped_adapter = self.__wrapped__
        wrapped_adapter.spawn(**kwargs)
    else:
        # original spawn logic
        pass