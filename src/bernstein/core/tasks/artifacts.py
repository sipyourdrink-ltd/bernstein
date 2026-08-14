import json

def _canonical_finding_bytes(self, raw):
    """Convert a finding dict to canonical bytes for content addressing.
    
    Ensures deterministic serialization by recursively sorting keys
    and using stable JSON encoding.
    """
    if not isinstance(raw, dict):
        raise ValueError('Input must be a dictionary')
    
    # Serialize with sorted keys for deterministic output
    canonical_json = json.dumps(raw, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
    return canonical_json.encode('utf-8')
