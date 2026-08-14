def _canonical_finding_bytes(self, raw):
    if not isinstance(raw, dict):
        raise ValueError('Input must be a dictionary')
    if not raw:
        raise ValueError('Input dictionary cannot be empty')
    return {k: str(v) for k, v in raw.items()}
