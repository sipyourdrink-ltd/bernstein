#!/usr/bin/env python3
"""Simple test for scanner_finding without pytest."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from bernstein.adapters.scanner_finding import Finding, _canonical_json_bytes, findings_hash

def test_finding_basic():
    """Test basic Finding functionality."""
    print("Testing Finding basic functionality...")
    
    # Test initialization
    f = Finding(rule="test-rule", path="/some/path")
    assert f.rule == "test-rule"
    assert f.path == "/some/path"
    assert f.severity == "informational"
    assert f.summary == ""
    assert f.extra == {}
    print("✓ Finding initialization works")
    
    # Test to_dict
    d = f.to_dict()
    expected = {
        "rule": "test-rule",
        "path": "/some/path",
        "severity": "informational",
        "summary": "",
    }
    assert d == expected
    print("✓ Finding.to_dict works")
    
    # Test from_dict
    data = {
        "rule": "test-rule",
        "path": "/some/path",
        "severity": "high",
        "summary": "Test summary",
        "extra": {"key": "value"},
    }
    f2 = Finding.from_dict(data)
    assert f2.rule == "test-rule"
    assert f2.path == "/some/path"
    assert f2.severity == "high"
    assert f2.summary == "Test summary"
    assert f2.extra == {"key": "value"}
    print("✓ Finding.from_dict works")
    
    # Test frozen
    try:
        f.rule = "changed"  # type: ignore
        assert False, "Should not be able to modify"
    except AttributeError:
        pass
    print("✓ Finding is frozen")
    
    # Test hash
    hash_val = f.finding_hash()
    assert isinstance(hash_val, str)
    assert len(hash_val) == 64  # SHA-256
    print("✓ Finding.finding_hash works")
    
    # Test findings_hash
    f1 = Finding(rule="a", path="p1")
    f2 = Finding(rule="b", path="p2")
    f3 = Finding(rule="c", path="p3")
    h1 = findings_hash([f1, f2, f3])
    h2 = findings_hash([f3, f1, f2])
    assert h1 == h2
    print("✓ findings_hash works (order independent)")
    
    print("\nAll tests passed!")

if __name__ == "__main__":
    test_finding_basic()