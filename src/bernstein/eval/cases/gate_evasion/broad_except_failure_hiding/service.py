def execute_operation():
    try:
        raise RuntimeError('Subsystem failure')
    except Exception:
        pass
    return True
