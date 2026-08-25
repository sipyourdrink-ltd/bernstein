#!/usr/bin/env python3
"""Debug script to check actual exit codes."""

import subprocess
import tempfile
from pathlib import Path

def test_shell_exit_code():
    """Test what exit code we get from shell for nonexistent command."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cwd = Path(tmpdir)
        
        try:
            proc = subprocess.run(
                "nonexistentcommand12345",
                shell=True,
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            print(f"Exit code: {proc.returncode}")
            print(f"Stdout: {proc.stdout}")
            print(f"Stderr: {proc.stderr}")
            return proc.returncode
        except Exception as e:
            print(f"Exception: {e}")
            return None

if __name__ == "__main__":
    exit_code = test_shell_exit_code()
    print(f"Final exit code: {exit_code}")