#!/usr/bin/env python3
"""Debug floating-point precision."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent / "src"))

import time as _time

now = _time.time()
horizon = 7 * 24 * 3600  # 7 days
ts = now - horizon

age = now - ts
print(f"now = {now}")
print(f"horizon = {horizon}")
print(f"ts = {ts}")
print(f"age = {age}")
print(f"age <= horizon? {age <= horizon}")
print(f"age <= horizon + 1e-6? {age <= horizon + 1e-6}")
print(f"age - horizon = {age - horizon}")

# Now simulate what the test does
test_now = _time.time()
test_ts = test_now - horizon
test_age = now - test_ts  # note: using the function's now, not test's now
print(f"\n--- Test simulation ---")
print(f"test_now = {test_now}")
print(f"test_ts = {test_ts}")
print(f"test_age (using function's now) = {test_age}")
print(f"test_age <= horizon? {test_age <= horizon}")
print(f"test_age - horizon = {test_age - horizon}")
