# 347c — Organizational Rule Enforcement Layer
**Role:** backend  **Priority:** 2 (high)  **Scope:** medium

## Problem
388 upvotes: "If your agent doesn't follow company rules, code goes off the rails."

## Design
Single .bernstein/rules.yaml with organizational standards. Auto-checked on every agent completion. Violations block merge with actionable message.
