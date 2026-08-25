#!/usr/bin/env python3
"""Test script to verify exit code 127 detection."""

import tempfile
from pathlib import Path
from bernstein.core.quality.quality_gates import _run_command

def test_exit_code_127():
    """Test that exit code 127 is detected and handled correctly."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cwd = Path(tmpdir)
        
        # Test with a command that doesn't exist (should return exit code 127)
        result = _run_command("nonexistentcommand12345", cwd, 10)
        
        print(f"Result type: {type(result)}")
        print(f"Result length: {len(result)}")
        print(f"Result: {result}")
        
        if len(result) == 3:
            ok, output, exit_code = result
            print(f"Exit code detected: {exit_code}")
            if exit_code == 127:
                print("✓ SUCCESS: Exit code 127 correctly detected")
                return True
            else:
                print(f"✗ FAIL: Expected exit code 127, got {exit_code}")
                return False
        else:
            print("✗ FAIL: Expected 3-tuple, got different length")
            return False

def test_normal_command():
    """Test that normal commands still work correctly."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cwd = Path(tmpdir)
        
        # Test with a command that should succeed
        result = _run_command("echo 'hello world'", cwd, 10)
        
        print(f"Normal command result type: {type(result)}")
        print(f"Normal command result length: {len(result)}")
        print(f"Normal command result: {result}")
        
        if len(result) == 2:
            ok, output = result
            if ok and "hello world" in output:
                print("✓ SUCCESS: Normal command works correctly")
                return True
            else:
                print(f"✗ FAIL: Normal command failed: ok={ok}, output={output}")
                return False
        else:
            print("✗ FAIL: Normal command should return 2-tuple")
            return False

if __name__ == "__main__":
    print("Testing exit code 127 detection...")
    test1 = test_exit_code_127()
    print()
    print("Testing normal command...")
    test2 = test_normal_command()
    
    if test1 and test2:
        print("\n✓ All tests passed!")
    else:
        print("\n✗ Some tests failed!")