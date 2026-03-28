# 334c — Automated Quality Gates Between Agent Steps
**Role:** backend  **Priority:** 0 (urgent)  **Scope:** medium

## Problem
5+ sources: 1.7x more issues in AI PRs. No checkpoints between agent steps.

## Design
After each task: lint gate, type gate, test gate, optional mutation testing, optional cross-model review. Configurable per-task in bernstein.yaml.
