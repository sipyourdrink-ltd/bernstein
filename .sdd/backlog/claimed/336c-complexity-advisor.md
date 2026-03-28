# 336c — Complexity Advisor (Single vs Multi-Agent)
**Role:** backend  **Priority:** 2 (high)  **Scope:** small

## Problem
"Most teams skip to multi-agent because it looks impressive. Then debugging coordination failures."

## Design
Before decomposing, evaluate: is this parallelizable? Would single-agent be faster? If task touches <5 files with heavy cross-file deps → single agent mode. --force-parallel override available.
